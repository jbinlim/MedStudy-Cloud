import os

# generation_worker.py와 같은 이유로, 다른 모듈을 임포트하기 전에
# 표준 입출력(0,1,2) 외의 상속받은 파일 디스크립터를 전부 닫는다.
try:
    _MAX_FD = os.sysconf("SC_OPEN_MAX")
except (ValueError, OSError):
    _MAX_FD = 1024
os.closerange(3, _MAX_FD)

import argparse
import json
import time
from pathlib import Path

from database import (
    claim_next_drive_sync_job,
    enqueue_generation_job,
    finish_drive_sync_job,
    init_db,
    reset_interrupted_drive_sync_jobs,
)
from drive_sync import sync_candidates


BASE_DIR = Path(__file__).resolve().parent
PID_PATH = BASE_DIR / ".drive_sync_worker.pid"


def _process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def process_sync_job(job):
    try:
        result = sync_candidates(
            json.loads(job["candidates_json"]),
            job["data_dir"],
        )
        queued_count = 0
        if job["auto_generate"]:
            for generation_job in result["generation_jobs"]:
                _, created = enqueue_generation_job(generation_job["lecture_id"])
                queued_count += int(created)
        saved_result = {
            key: value for key, value in result.items()
            if key != "generation_jobs"
        }
        saved_result["queued_count"] = queued_count
    except Exception as error:
        finish_drive_sync_job(job["id"], error=error)
    else:
        finish_drive_sync_job(job["id"], result=saved_result)


def run_worker(parent_pid):
    init_db()
    reset_interrupted_drive_sync_jobs()
    idle_since = time.monotonic()
    while True:
        job = claim_next_drive_sync_job()
        if job is None:
            if not _process_exists(parent_pid) and time.monotonic() - idle_since > 10:
                return
            time.sleep(1)
            continue
        idle_since = time.monotonic()
        process_sync_job(job)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    try:
        run_worker(args.parent_pid)
    finally:
        try:
            if PID_PATH.exists() and PID_PATH.read_text().strip() == str(os.getpid()):
                PID_PATH.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
