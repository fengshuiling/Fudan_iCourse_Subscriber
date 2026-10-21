"""Optional CI budget; the local/GitHub pipeline is unlimited by default."""

import os
import time


def exhausted() -> bool:
    deadline = float(os.environ.get("ICOURSE_DEADLINE_EPOCH", "0"))
    return bool(deadline and time.time() >= deadline)


def lecture_batch(lectures: list) -> list:
    limit = int(os.environ.get("ICOURSE_MAX_LECTURES", "0"))
    if limit < 0:
        raise ValueError("ICOURSE_MAX_LECTURES must be nonnegative")
    return lectures[:limit] if limit else lectures
