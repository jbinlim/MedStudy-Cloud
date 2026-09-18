import os

# 부모(streamlit) 프로세스가 열어둔 네트워크 소켓 같은 파일 디스크립터를
# 이 자식 프로세스가 그대로 물려받지 않도록, 다른 모듈을 임포트하기 전에
# 표준 입출력(0,1,2) 외에는 전부 닫는다. `os.posix_spawn`으로 띄우면
# close-on-exec가 안 걸린 일부 fd(예: streamlit의 리스닝 소켓)가 그대로
# 넘어와서, streamlit이 강제 종료된 뒤에도 이 워커가 포트를 붙잡고 있어
# 다음 실행이 "포트를 쓸 수 없다"며 실패하는 문제가 있었다.
try:
    _MAX_FD = os.sysconf("SC_OPEN_MAX")
except (ValueError, OSError):
    _MAX_FD = 1024
os.closerange(3, _MAX_FD)

import argparse
import time
from pathlib import Path

from automation_service import run_full_automation
from codex_service import find_codex_cli, get_codex_login_status
from cloud_sync import try_sync_lecture_to_cloud
from database import (
    claim_next_generation_job,
    finish_generation_job,
    init_db,
    reset_interrupted_generation_jobs,
    update_generation_job,
)


BASE_DIR = Path(__file__).resolve().parent
OCR_DATA_DIR = BASE_DIR / "ocr_data"
PID_PATH = BASE_DIR / ".generation_worker.pid"


def _friendly_generation_error(error):
    """긴 Codex 명령 전체 대신 사용자가 조치할 수 있는 오류만 표시한다."""
    message = str(error)
    lowered = message.lower()
    if "usage limit" in lowered or "hit your usage limit" in lowered:
        return (
            "Codex 사용량 한도에 도달했습니다. "
            "사용량이 갱신된 후 다시 시도해주세요."
        )
    if "timed out" in lowered or "timeout" in lowered:
        return (
            "AI 응답 시간이 초과되었습니다. "
            "전사문을 더 작은 구간으로 나누어 다시 시도해주세요."
        )
    return message[-2000:]


def _process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _write_generation_state(pdf_path, state):
    if pdf_path:
        Path(pdf_path).parent.joinpath("generation_state.txt").write_text(
            state,
            encoding="utf-8",
        )


def process_generation_job(job):
    """확보한 작업 하나를 처리하고 결과 상태를 DB에 저장한다."""
    job_id = job["job_id"]
    try:
        if not job["pdf_path"] or not Path(job["pdf_path"]).exists():
            raise FileNotFoundError("강의록 PDF 파일을 찾을 수 없습니다.")
        transcript_path = job["transcript_path"]
        if not transcript_path or not Path(transcript_path).exists():
            raise FileNotFoundError("전사문 TXT 파일을 찾을 수 없습니다.")

        codex_path = find_codex_cli()
        if not codex_path:
            raise RuntimeError("Codex CLI를 찾지 못했습니다.")
        logged_in, login_message = get_codex_login_status(codex_path)
        if not logged_in:
            raise RuntimeError(f"Codex 로그인이 필요합니다. {login_message}")

        result = run_full_automation(
            lecture_id=job["lecture_id"],
            subject_name=job["subject_name"],
            lecture_no=job["lecture_no"],
            lecture_title=job["lecture_title"],
            pdf_path=job["pdf_path"],
            transcript_path=transcript_path,
            ocr_data_dir=OCR_DATA_DIR,
            codex_path=codex_path,
            progress_callback=lambda progress, stage, message: update_generation_job(
                job_id, progress, stage, message
            ),
        )
    except Exception as error:
        finish_generation_job(
            job_id,
            error=RuntimeError(_friendly_generation_error(error)),
        )
        _write_generation_state(job["pdf_path"], "pending")
    else:
        finish_generation_job(job_id, warnings=result.get("warnings"))
        _write_generation_state(job["pdf_path"], "complete")
        # 클라우드 설정이 완료된 환경에서는 새 정리본과 문제를 자동 복제한다.
        # 클라우드 오류가 로컬 정리본 생성을 실패로 바꾸지 않도록 분리한다.
        try:
            try_sync_lecture_to_cloud(job["lecture_id"])
        except Exception as error:
            print(f"클라우드 자동 동기화 실패: {error}", flush=True)


def run_worker(parent_pid):
    init_db()
    reset_interrupted_generation_jobs()
    idle_since = time.monotonic()

    while True:
        job = claim_next_generation_job()
        if job is None:
            # 앱 서버가 종료된 뒤에는 새 요청이 생기지 않으므로 잠시 후 종료한다.
            if not _process_exists(parent_pid) and time.monotonic() - idle_since > 10:
                return
            time.sleep(1)
            continue

        idle_since = time.monotonic()
        process_generation_job(job)


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
