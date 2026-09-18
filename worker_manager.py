import fcntl
import os
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PID_PATH = BASE_DIR / ".generation_worker.pid"
LOG_PATH = BASE_DIR / "generation_worker.log"
SYNC_PID_PATH = BASE_DIR / ".drive_sync_worker.pid"
SYNC_LOG_PATH = BASE_DIR / "drive_sync_worker.log"


def _spawn_detached_worker(args, log_path):
    """`subprocess.Popen(..., start_new_session=True)` 대신 `os.posix_spawn`으로
    직접 띄운다.

    이 앱(Streamlit)처럼 멀티스레드+네트워킹을 쓰는 프로세스에서
    `start_new_session=True`나 `cwd=`가 있으면 CPython의 `subprocess`가
    `posix_spawn` 대신 옛 `fork()+exec()` 경로로 되돌아간다. 특정 macOS
    업데이트 이후 이 경로에서 fork() 직후 Network.framework의 atfork
    핸들러가 자식 프로세스를 크래시시키는 버그가 생겨(EXC_BAD_ACCESS in
    NEFlowDirectorDestroy), 워커를 띄우려 할 때마다 앱이 죽었다.
    `os.posix_spawn`은 fork()를 아예 쓰지 않으면서도 `setsid=True`로 같은
    세션 분리 효과를 낼 수 있어 이 크래시를 완전히 피한다.
    """
    log_fd_actions = [
        (os.POSIX_SPAWN_OPEN, 1, str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644),
        (os.POSIX_SPAWN_DUP2, 1, 2),
    ]
    return os.posix_spawn(
        sys.executable,
        [sys.executable, *args],
        dict(os.environ),
        file_actions=log_fd_actions,
        setsid=True,
    )


def _process_exists(pid):
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        # 현재 프로세스의 자식이 아니면 일반 존재 여부 검사로 이어간다.
        pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def ensure_generation_worker():
    """백그라운드 생성 프로세스가 없을 때 하나만 실행한다."""
    with PID_PATH.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            existing_pid = int(PID_PATH.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            existing_pid = None

        if existing_pid and _process_exists(existing_pid):
            return existing_pid

        pid = _spawn_detached_worker(
            [
                str(BASE_DIR / "generation_worker.py"),
                "--parent-pid",
                str(os.getpid()),
            ],
            LOG_PATH,
        )
        PID_PATH.write_text(str(pid), encoding="utf-8")
        return pid


def ensure_drive_sync_worker():
    """정리본 생성과 독립적으로 Drive 동기화 작업자를 실행한다."""
    with SYNC_PID_PATH.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            existing_pid = int(SYNC_PID_PATH.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            existing_pid = None

        if existing_pid and _process_exists(existing_pid):
            return existing_pid

        pid = _spawn_detached_worker(
            [
                str(BASE_DIR / "drive_sync_worker.py"),
                "--parent-pid",
                str(os.getpid()),
            ],
            SYNC_LOG_PATH,
        )
        SYNC_PID_PATH.write_text(str(pid), encoding="utf-8")
        return pid
