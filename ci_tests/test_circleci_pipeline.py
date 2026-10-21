"""Offline tests. All databases, identities, remotes and keys are synthetic."""

import gzip
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts import circleci_pipeline as ci
from src.data import crypto_box, sharder
from src.data.schema import SCHEMA_SQL
from src.runtime import run_budget


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "STUID": "test-student", "UISPSW": "test-password",
            "COURSE_IDS": "1", "SUBSCRIBED_COURSE_IDS": "1",
            "DASHSCOPE_API_KEY": "test-model-key", "SMTP_PASSWORD": "test-smtp",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pw = crypto_box.derive_new_password("test-student", "test-password")

    def db(self, name="source.db", lectures=("a",)):
        path = self.root / name
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute("INSERT INTO courses VALUES ('1', 'Synthetic course', 'Teacher')")
            for lecture in lectures:
                conn.execute("INSERT INTO lectures (sub_id,course_id,transcript) VALUES (?, '1', ?)", (lecture, "text " + lecture))
            conn.execute("INSERT INTO ppt_pages (sub_id,page_num,created_sec,text) VALUES (?,1,0,'OCR')", (lectures[0],))
            conn.execute("INSERT INTO all_courses VALUES ('1','test-term','Title','Teacher','Dept','2026-01-01')")
        return path

    def snapshot(self, source, name="snapshot"):
        folder = self.root / name
        folder.mkdir()
        sharder.shard_database(str(source), str(folder), self.pw)
        return {"data/" + str(p.relative_to(folder)).replace(os.sep, "/"): p.read_bytes() for p in folder.rglob("*") if p.is_file()}

    def decode(self, files, name="decoded"):
        out = self.root / (name + ".db")
        ci.decode_snapshot(files.__getitem__, set(files), out, self.root / name)
        return out

    def ids(self, db):
        with closing(sqlite3.connect(db)) as conn:
            return {r[0] for r in conn.execute("SELECT sub_id FROM lectures")}

    def test_sharded_roundtrip_keeps_all_rows_and_psa(self):
        source = self.db()
        restored = self.decode(self.snapshot(source))
        ci.assert_preserved(source, restored)

    def test_wrong_password_does_not_replace_destination(self):
        files = self.snapshot(self.db())
        target = self.root / "decoded.db"
        target.write_bytes(b"existing destination")
        with patch.dict(os.environ, {"UISPSW": "wrong"}):
            with self.assertRaises(ci.SafetyError):
                self.decode(files)
        self.assertEqual(target.read_bytes(), b"existing destination")

    def test_missing_corrupted_and_truncated_shards_abort(self):
        source = self.snapshot(self.db())
        name = next(p for p in source if p.startswith("data/shards/"))
        for mode in ("missing", "corrupt", "truncate"):
            with self.subTest(mode=mode):
                files = source.copy()
                if mode == "missing":
                    del files[name]
                elif mode == "corrupt":
                    blob = bytearray(files[name]); blob[32] ^= 1; files[name] = bytes(blob)
                else:
                    files[name] = files[name][:-16]
                with self.assertRaises(ci.SafetyError):
                    self.decode(files, mode)
                self.assertFalse((self.root / (mode + ".db")).exists())

    def test_bad_index_never_falls_back_to_legacy(self):
        files = {"data/icourse-index.enc": b"broken", "data/icourse.db.enc": crypto_box.encrypt(self.db().read_bytes(), self.pw)}
        with self.assertRaises(ci.SafetyError):
            self.decode(files)

    def test_empty_unknown_duplicate_and_unsafe_manifests_abort(self):
        original = self.snapshot(self.db())
        index = json.loads(crypto_box.decrypt(original["data/icourse-index.enc"], self.pw))
        for mode in ("empty", "version", "duplicate", "traversal", "checksum"):
            obj = json.loads(json.dumps(index))
            if mode == "empty": obj["shards"] = []
            if mode == "version": obj["version"] = 999
            if mode == "duplicate": obj["shards"].append(obj["shards"][0])
            if mode == "traversal": obj["shards"][0]["name"] = "../escape.db.gz.enc"
            if mode == "checksum": del obj["shards"][0]["sha256"]
            files = original | {"data/icourse-index.enc": crypto_box.encrypt(json.dumps(obj).encode(), self.pw)}
            with self.subTest(mode=mode), self.assertRaises(ci.SafetyError):
                self.decode(files, mode)

    def test_missing_history_and_invalid_sqlite_abort(self):
        for i, files in enumerate(({}, {"data/icourse.db.enc": crypto_box.encrypt(b"SQLite format 3\0garbage", self.pw)})):
            with self.subTest(i=i), self.assertRaises(ci.SafetyError):
                self.decode(files, str(i))

    def test_legacy_keys_and_compression_are_compatible(self):
        source = self.db()
        for version, count in (("v2", 100000), ("legacy", 10000)):
            pw = self.pw if version == "v2" else crypto_box.derive_legacy_password("test-student", "test-password", "test-model-key", "test-smtp")
            for compressed in (False, True):
                raw = gzip.compress(source.read_bytes()) if compressed else source.read_bytes()
                name = "data/icourse.db.gz.enc" if compressed else "data/icourse.db.enc"
                restored = self.decode({name: crypto_box.encrypt(raw, pw, count)}, f"{version}-{compressed}")
                self.assertEqual(self.ids(restored), {"a"})

    def test_sqlite_backup_includes_wal(self):
        source = self.db()
        with closing(sqlite3.connect(source)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO lectures (sub_id,course_id) VALUES ('wal','1')")
            conn.commit()
            ci.backup_db(source, self.root / "backup.db")
            self.assertEqual(self.ids(self.root / "backup.db"), {"a", "wal"})

    def git(self, cwd, *args):
        result = subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args], cwd=cwd, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.decode().strip()

    def remote(self):
        bare = self.root / "remote.git"
        bare.mkdir()
        self.git(bare, "init", "--bare", "-q")
        seed = self.root / "seed"
        seed.mkdir()
        self.git(seed, "init", "-q", "-b", "data")
        files = self.snapshot(self.db())
        for name, blob in files.items():
            path = seed / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(blob)
        (seed / "README.md").write_text("preserve unrelated files")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-qm", "seed")
        self.git(seed, "remote", "add", "origin", str(bare))
        self.git(seed, "push", "-q", "origin", "data")
        return bare, seed

    def test_publish_preserves_remote_only_rows_and_uses_parent_commit(self):
        bare, _seed = self.remote()
        before = self.git(bare, "rev-parse", "refs/heads/data")
        local = self.db("local-source.db", ("local",))
        branch = ci.DataBranch(self.root / "worker", "test/repo", remote_url=str(bare))
        branch.publish(local, self.root)
        after = self.git(bare, "rev-parse", "refs/heads/data")
        self.assertNotEqual(before, after)
        self.assertEqual(self.git(bare, "rev-parse", after + "^"), before)
        branch.decode(after, self.root / "final.db", self.root / "final")
        self.assertEqual(self.ids(self.root / "final.db"), {"a", "local"})
        self.assertEqual(self.git(bare, "show", "data:README.md"), "preserve unrelated files")
        paths = self.git(bare, "ls-tree", "-r", "--name-only", "data").splitlines()
        self.assertTrue(all(p == "README.md" or p.endswith(".enc") for p in paths))

    def test_concurrent_update_is_redecoded_and_merged_before_retry(self):
        bare, seed = self.remote()
        branch = ci.DataBranch(self.root / "worker", "test/repo", remote_url=str(bare))
        original_git = branch.git
        pushed = []
        def race(*args, **kwargs):
            if args[0] == "push" and not pushed:
                files = self.snapshot(self.db("concurrent.db", ("concurrent",)), "concurrent-snapshot")
                for name, blob in files.items(): (seed / name).write_bytes(blob)
                self.git(seed, "add", "."); self.git(seed, "commit", "-qm", "concurrent writer")
                self.git(seed, "push", "-q", "origin", "data")
                pushed.append(True)
            return original_git(*args, **kwargs)
        with patch.object(branch, "git", side_effect=race):
            branch.publish(self.db("local-source.db", ("local",)), self.root)
        sha = branch.fetch()
        branch.decode(sha, self.root / "final.db", self.root / "final")
        # The concurrent writer deliberately replaced its own 'a' row. The
        # latest history, rather than our stale snapshot, must be the base.
        self.assertEqual(self.ids(self.root / "final.db"), {"concurrent", "local"})

    def test_decryption_failure_during_publish_never_pushes(self):
        bare, seed = self.remote()
        (seed / "data/icourse-index.enc").write_bytes(b"corrupted")
        self.git(seed, "add", "."); self.git(seed, "commit", "-qm", "corruption")
        self.git(seed, "push", "-q", "origin", "data")
        before = self.git(bare, "rev-parse", "refs/heads/data")
        branch = ci.DataBranch(self.root / "worker", "test/repo", remote_url=str(bare))
        with patch.object(branch, "git", wraps=branch.git) as calls:
            with self.assertRaises(ci.SafetyError):
                branch.publish(self.db("local-source.db"), self.root)
            self.assertFalse(any(c.args[0] == "push" for c in calls.call_args_list))
        self.assertEqual(self.git(bare, "rev-parse", "refs/heads/data"), before)

    def test_missing_branch_is_fatal(self):
        bare = self.root / "empty.git"; bare.mkdir()
        self.git(bare, "init", "--bare", "-q")
        branch = ci.DataBranch(self.root / "worker", "test/repo", remote_url=str(bare))
        with self.assertRaises(ci.SafetyError): branch.fetch()


class GateTests(unittest.TestCase):
    def test_credential_aliases_and_conflicts(self):
        for env in ({"STUID": "s", "UISPSW": "p"}, {"StuId": "s", "UISPsw": "p"}):
            ci.normalize_credentials(env)
            self.assertEqual(env["STUID"], env["StuId"])
            self.assertEqual(env["UISPSW"], env["UISPsw"])
        with self.assertRaises(ci.SafetyError):
            ci.normalize_credentials({"STUID": "s", "StuId": "different", "UISPSW": "p"})

    def test_full_run_stops_without_owner_opt_in(self):
        with patch.dict(os.environ, {"STUID": "s", "UISPSW": "p", "GH_REPOSITORY": "test/repo", "GH_PAT": "test", "CIRCLE_BRANCH": "main"}, clear=True):
            with self.assertRaisesRegex(ci.SafetyError, "ICOURSE_ENABLE_RUN"):
                ci.preflight()
            self.assertEqual(ci.preflight(read_only=True), "test/repo")

    def test_github_token_is_not_a_pat_fallback(self):
        with patch.dict(os.environ, {"STUID": "s", "UISPSW": "p", "GH_REPOSITORY": "test/repo", "GITHUB_TOKEN": "unused", "CIRCLE_BRANCH": "main"}, clear=True):
            with self.assertRaisesRegex(ci.SafetyError, "GH_PAT"):
                ci.preflight(read_only=True)

    def test_budget_limits_batch_and_leaves_default_unlimited(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(run_budget.lecture_batch([1, 2, 3]), [1, 2, 3])
            self.assertFalse(run_budget.exhausted())
        with patch.dict(os.environ, {"ICOURSE_MAX_LECTURES": "2", "ICOURSE_DEADLINE_EPOCH": "1"}):
            self.assertEqual(run_budget.lecture_batch([1, 2, 3]), [1, 2])
            self.assertTrue(run_budget.exhausted())

    def test_config_has_no_default_crawl_or_plaintext_cache(self):
        config = yaml.safe_load((ci.ROOT / ".circleci/config.yml").read_text())
        self.assertFalse(config["parameters"]["run_course_check"]["default"])
        self.assertFalse(config["parameters"]["verify_database"]["default"])
        self.assertEqual(config["jobs"]["course-check"]["resource_class"], "medium")
        steps = config["jobs"]["course-check"]["steps"]
        for step in steps:
            if isinstance(step, dict):
                self.assertFalse({"store_artifacts", "persist_to_workspace"} & step.keys())
                if "save_cache" in step:
                    self.assertNotIn("data", step["save_cache"]["paths"])


if __name__ == "__main__":
    unittest.main()
