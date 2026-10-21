#!/usr/bin/env python3
"""Fail-closed CircleCI adapter around the existing crawler/sharder/merger.

verify only reads/decrypts history; execute requires two explicit opt-ins.
No command initializes a missing data branch or uses GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SHARD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.db\.gz\.enc")
TABLES = ("courses", "lectures", "ppt_pages", "all_courses", "meta")


class SafetyError(RuntimeError):
    pass


def normalize_credentials(env: dict) -> None:
    for canonical, old in (("STUID", "StuId"), ("UISPSW", "UISPsw")):
        upper, mixed = env.get(canonical, ""), env.get(old, "")
        if upper and mixed and upper != mixed:
            raise SafetyError(f"Conflicting values for {canonical} and {old}")
        value = upper or mixed
        if not value:
            raise SafetyError(f"Missing {canonical}")
        env[canonical] = env[old] = value


def preflight(*, read_only: bool = False) -> str:
    normalize_credentials(os.environ)
    repo = os.environ.get("GH_REPOSITORY") or "/".join((
        os.environ.get("CIRCLE_PROJECT_USERNAME", ""),
        os.environ.get("CIRCLE_PROJECT_REPONAME", ""),
    ))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise SafetyError("Set GH_REPOSITORY=owner/repository")
    if not os.environ.get("GH_PAT"):
        raise SafetyError("Missing GH_PAT (GITHUB_TOKEN is never used)")
    if os.environ.get("CIRCLE_BRANCH") != "main":
        raise SafetyError("Only the main code branch is permitted")
    if os.environ.get("CIRCLE_PULL_REQUEST") or os.environ.get("CIRCLE_PR_NUMBER"):
        raise SafetyError("Refusing credentialed execution on a pull request")
    if read_only:
        return repo
    for name in ("ICOURSE_ENABLE_RUN", "ICOURSE_ENABLE_DATA_WRITE"):
        if os.environ.get(name) != "true":
            raise SafetyError(f"{name}=true is required after owner confirmation")
    for name in ("COURSE_IDS", "SMTP_EMAIL", "SMTP_PASSWORD", "RECEIVER_EMAIL"):
        if not os.environ.get(name, "").strip():
            raise SafetyError(f"Missing {name}")
    from src.runtime import config
    if not config.resolve_model_providers():
        raise SafetyError("Set at least one MODEL_PROVIDERS API key")
    if Path(config.DB_PATH).resolve() != ROOT / "data" / "icourse.db":
        raise SafetyError("CircleCI requires DB_PATH=data/icourse.db")
    if Path(config.DATA_DIR).resolve() != ROOT / "data":
        raise SafetyError("CircleCI requires DATA_DIR=data")
    if config.ASR_BACKEND != "sensevoice":
        raise SafetyError("Configure the model installer/cache before changing ASR_BACKEND")
    if int(os.environ.get("ICOURSE_MAX_LECTURES", "2")) < 1:
        raise SafetyError("CircleCI requires a positive ICOURSE_MAX_LECTURES")
    return repo


def validate_db(path: Path) -> None:
    if not path.is_file():
        raise SafetyError("Database file is missing")
    # mode=ro prevents sqlite from silently creating an empty database.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise SafetyError("SQLite integrity check failed")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"courses", "lectures"}.issubset(tables):
            raise SafetyError("Database is missing core tables")


def backup_db(source: Path, target: Path) -> None:
    validate_db(source)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)


def migrate_db(path: Path) -> None:
    from src.data.database import Database
    db = Database(str(path))
    db.conn.close()
    validate_db(path)


def validate_manifest(index: dict) -> list[dict]:
    from src.data.sharder import INDEX_VERSION
    if not isinstance(index, dict) or type(index.get("version")) is not int:
        raise SafetyError("Invalid shard index")
    if not 1 <= index["version"] <= INDEX_VERSION:
        raise SafetyError("Unsupported shard index version")
    entries = index.get("shards")
    if not isinstance(entries, list) or not entries:
        raise SafetyError("Missing/empty shard manifest; refusing an empty history")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SafetyError("Invalid shard entry")
        name = entry.get("name", "")
        if not isinstance(name, str) or not SHARD_NAME.fullmatch(name) or name in seen:
            raise SafetyError("Unsafe or duplicate shard filename")
        seen.add(name)
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise SafetyError("Missing shard checksum")
        if type(entry.get("size")) is not int or entry["size"] <= 0:
            raise SafetyError("Invalid shard size")
    return entries


def decode_snapshot(read_file, paths: set[str], output: Path, scratch: Path) -> None:
    """Read a single immutable Git commit; never fall back after format errors."""
    from src.data import crypto_box, sharder
    pw = crypto_box.derive_new_password(os.environ["STUID"], os.environ["UISPSW"])
    scratch.mkdir(parents=True)
    staged = scratch / "history.db"
    try:
        if "data/icourse-index.enc" in paths:
            index_file = scratch / sharder.INDEX_FILENAME
            index_file.write_bytes(read_file("data/icourse-index.enc"))
            index = sharder.load_index(str(index_file), pw)
            shards = scratch / sharder.SHARDS_DIR
            shards.mkdir()
            for entry in validate_manifest(index):
                remote_path = "data/shards/" + entry["name"]
                if remote_path not in paths:
                    raise SafetyError("A referenced shard is missing")
                blob = read_file(remote_path)
                if len(blob) != entry["size"] or hashlib.sha256(blob).hexdigest() != entry["sha256"]:
                    raise SafetyError("Shard size/checksum mismatch")
                # Validate each SQLite too; do not let INSERT OR IGNORE hide
                # an invalid shard before rebuilding the combined database.
                raw = gzip.decompress(crypto_box.decrypt(blob, pw))
                shard_db = scratch / "validate.db"
                shard_db.write_bytes(raw)
                validate_db(shard_db)
                shard_db.unlink()
                (shards / entry["name"]).write_bytes(blob)
            sharder.reassemble_database(index, str(shards), str(staged), pw)
        else:
            legacy = next((p for p in ("data/icourse.db.gz.enc", "data/icourse.db.enc") if p in paths), None)
            if legacy is None:
                raise SafetyError("No recognized database on existing data branch")
            compressed = legacy.endswith(".gz.enc")
            raw, _version = crypto_box.decrypt_with_fallback(
                read_file(legacy), stuid=os.environ["STUID"], uispsw=os.environ["UISPSW"],
                dashscope=os.environ.get("DASHSCOPE_API_KEY", ""),
                smtp=os.environ.get("SMTP_PASSWORD", ""),
                validate=crypto_box.is_gzip if compressed else crypto_box.is_sqlite,
            )
            staged.write_bytes(gzip.decompress(raw) if compressed else raw)
        validate_db(staged)
        migrate_db(staged)
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, output)  # No partial destination on decryption failure.
    except Exception as exc:
        raise SafetyError("History read/decryption/validation failed; no publication is permitted") from exc


def assert_preserved(source: Path, restored: Path) -> None:
    """Check actual rows after shard/reassemble, including metadata and PPTs."""
    with closing(sqlite3.connect(source)) as left, closing(sqlite3.connect(restored)) as right:
        for table in TABLES:
            cols = [r[1] for r in left.execute(f"PRAGMA table_info({table})")]
            other = [r[1] for r in right.execute(f"PRAGMA table_info({table})")]
            if cols != other:
                raise SafetyError(f"Round-trip schema mismatch: {table}")
            order = ",".join(str(i + 1) for i in range(len(cols)))
            a = left.execute(f"SELECT * FROM {table} ORDER BY {order}")
            b = right.execute(f"SELECT * FROM {table} ORDER BY {order}")
            while True:
                aa, bb = a.fetchmany(100), b.fetchmany(100)
                if aa != bb:
                    raise SafetyError(f"Round-trip row mismatch: {table}")
                if not aa:
                    break


class DataBranch:
    def __init__(self, directory: Path, repository: str, *, remote_url: str | None = None):
        self.directory = directory
        directory.mkdir()
        self.env = os.environ.copy()
        for name in ("GIT_TRACE", "GIT_TRACE_CURL", "GIT_CURL_VERBOSE", "GIT_CONFIG_COUNT"):
            self.env.pop(name, None)
        # The token never enters argv, git config, the remote URL, or logs.
        askpass = directory.parent / "git-askpass.sh"
        askpass.write_text('#!/bin/sh\ncase "$1" in\n*sername*) printf "%s" x-access-token ;;\n*) printf "%s" "$GH_PAT" ;;\nesac\n', encoding="utf-8")
        askpass.chmod(0o700)
        self.env.update(GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0")
        self.git("init", "-q")
        self.git("remote", "add", "origin", remote_url or f"https://github.com/{repository}.git")
        self.git("config", "user.name", "icourse-circleci")
        self.git("config", "user.email", "icourse-circleci@users.noreply.github.com")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.directory, env=self.env, capture_output=True, timeout=180,
        )
        if check and result.returncode:
            # Do not echo remote output: credential helpers/proxies may include secrets.
            raise SafetyError(f"Git {args[0]} failed; refusing to use stale/missing history")
        return result

    def fetch(self) -> str:
        # Missing branch, auth failure and network failure are all fatal.
        self.git("fetch", "--no-tags", "--depth=1", "origin", "+refs/heads/data:refs/remotes/origin/data")
        return self.git("rev-parse", "refs/remotes/origin/data").stdout.decode().strip()

    def decode(self, sha: str, output: Path, scratch: Path) -> None:
        paths = set()
        for record in self.git("ls-tree", "-rz", sha).stdout.split(b"\0"):
            if not record:
                continue
            info, path = record.split(b"\t", 1)
            name = path.decode("utf-8")
            if name.startswith("data/"):
                if info.split()[0] not in (b"100644", b"100755") or info.split()[1] != b"blob":
                    raise SafetyError("Non-regular file in data tree")
                paths.add(name)
        decode_snapshot(lambda path: self.git("show", f"{sha}:{path}").stdout, paths, output, scratch)

    def publish(self, local: Path, workspace: Path) -> None:
        from scripts.merge_db import merge
        from src.data import crypto_box, sharder
        backup_db(local, workspace / "local.db")  # Includes committed WAL pages.
        local = workspace / "local.db"
        for attempt in range(3):
            current = self.fetch()
            attempt_dir = workspace / f"publish-{attempt}"
            attempt_dir.mkdir()
            candidate = attempt_dir / "merged.db"
            self.decode(current, candidate, attempt_dir / "input")
            merge(str(local), str(candidate))
            validate_db(candidate)
            out = attempt_dir / "encrypted"
            out.mkdir()
            os.environ["SUBSCRIBED_COURSE_IDS"] = os.environ["COURSE_IDS"]
            pw = crypto_box.derive_new_password(os.environ["STUID"], os.environ["UISPSW"])
            sharder.shard_database(str(candidate), str(out), pw)
            roundtrip = attempt_dir / "roundtrip.db"
            paths = {"data/" + str(p.relative_to(out)).replace(os.sep, "/") for p in out.rglob("*") if p.is_file()}
            decode_snapshot(lambda path: (out / path.removeprefix("data/")).read_bytes(), paths, roundtrip, attempt_dir / "verify")
            assert_preserved(candidate, roundtrip)

            self.git("checkout", "-B", "data", current)
            data_dir = self.directory / "data"
            shards_dir = data_dir / "shards"
            shards_dir.mkdir(parents=True, exist_ok=True)
            for path in shards_dir.iterdir():
                if path.is_file() and SHARD_NAME.fullmatch(path.name):
                    path.unlink()
            for name in ("icourse.db.gz.enc", "icourse.db.enc"):
                (data_dir / name).unlink(missing_ok=True)
            shutil.copy2(out / sharder.INDEX_FILENAME, data_dir / sharder.INDEX_FILENAME)
            for path in (out / sharder.SHARDS_DIR).iterdir():
                shutil.copy2(path, shards_dir / path.name)
            self.git("add", "-A", "--", "data")
            changed = self.git("diff", "--cached", "--name-only", "-z").stdout.split(b"\0")
            for name in filter(None, changed):
                path = name.decode()
                if path not in ("data/icourse-index.enc", "data/icourse.db.gz.enc", "data/icourse.db.enc") and not (
                    path.startswith("data/shards/") and SHARD_NAME.fullmatch(path.removeprefix("data/shards/"))
                ):
                    raise SafetyError("Refusing to commit a non-database path")
            if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
                print("Encrypted database unchanged; no commit needed.", flush=True)
                return
            self.git("commit", "-q", "-m", "chore: update encrypted database via CircleCI [skip ci]")
            # A normal fast-forward push rejects concurrent updates. Re-fetch,
            # decode, merge and re-shard on retry. Never force-push or rebase blobs.
            result = self.git("push", "origin", "HEAD:refs/heads/data", check=False)
            if result.returncode == 0:
                print("Encrypted history published with a fast-forward commit.", flush=True)
                return
            print("Data push rejected; reloading history before retry.", flush=True)
        raise SafetyError("Data publication failed after 3 attempts; remote was not overwritten")


def run_main(seconds: int) -> None:
    env = os.environ.copy()
    # Stop starting new work 3 minutes before the child watchdog expires,
    # leaving room for its existing email and scheduler shutdown logic.
    env["ICOURSE_DEADLINE_EPOCH"] = str(time.time() + max(1, seconds - 180))
    env.setdefault("ICOURSE_MAX_LECTURES", "2")
    proc = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=ROOT, env=env, start_new_session=True)
    try:
        code = proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        raise SafetyError("Course pipeline exceeded its budget; publication is blocked")
    if code:
        raise SafetyError("Course pipeline failed; publication is blocked")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "verify", "execute"))
    args = parser.parse_args()
    os.chdir(ROOT)
    repository = preflight(read_only=args.operation == "verify")
    if args.operation == "preflight":
        print("Preflight passed; no course/data operations were performed.")
        return
    with tempfile.TemporaryDirectory(prefix="icourse-circleci-") as tmp:
        workspace = Path(tmp)
        remote = DataBranch(workspace / "repository", repository)
        sha = remote.fetch()
        decoded = workspace / "history.db"
        remote.decode(sha, decoded, workspace / "input")
        print(f"Historical database validated at {sha[:12]}.", flush=True)
        if args.operation == "verify":
            print("Read-only verification completed; no crawling, email, or push.")
            return
        started = int(os.environ.get("ICOURSE_JOB_STARTED_AT", str(int(time.time()))))
        remaining = int(3300 - (time.time() - started) - 300)
        if remaining < 300:
            raise SafetyError("Installation/read consumed the job budget; no crawl started")
        target = ROOT / "data" / "icourse.db"
        if target.exists():
            raise SafetyError("Unexpected local plaintext DB; refusing to reuse stale data")
        target.parent.mkdir(exist_ok=True)
        try:
            backup_db(decoded, target)
            run_main(remaining)
            validate_db(target)
            remote.publish(target, workspace)
        finally:
            for suffix in ("", "-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (SafetyError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        sys.exit(1)
