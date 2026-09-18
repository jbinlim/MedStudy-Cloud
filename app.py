import os
import shutil
import json
import re
import socket
import threading
from pathlib import Path
from time import monotonic, sleep

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from quiz_utils import (
    build_short_answer_quiz_section,
    extract_review_quiz,
    replace_review_quiz_section,
)

from codex_service import (
    find_codex_cli,
    get_codex_login_status,
    generate_summary_with_codex,
)
from transcript_service import correct_transcript_with_codex
from display_utils import format_korea_datetime
from drive_sync import (
    discover_google_drive_semesters,
    filter_actionable_candidates,
    scan_semester_folder,
)
from worker_manager import ensure_drive_sync_worker, ensure_generation_worker
from web_quiz_service import (
    build_additional_quiz_request,
    generate_additional_multiple_choice_quiz,
    validate_multiple_choice_quiz,
)
from cloud_sync import try_sync_lecture_to_cloud, try_sync_quiz_progress
from spaced_repetition import is_due
from exam_highlight_service import highlight_summary_with_codex
from ai_service import split_summary_sections
from pdf_export import PDF_EXPORT_VERSION, create_summary_pdf
from figure_service import (
    detect_figure_regions_with_codex,
    crop_detected_figures,
    select_figure_candidate_pages,
)
from pdf_utils import (
    extract_pdf_page_texts,
    process_pdf_pages,
)
from database import (
    init_db,
    add_subject,
    get_subjects,
    add_lecture,
    get_lectures,
    update_lecture_title,
    delete_lecture,
    update_lecture_pdf,
    update_lecture_transcript,
    update_corrected_transcript,
    replace_lecture_pages,
    get_lecture_pages,
    get_lecture_summary,
    get_recent_lecture_summaries,
    get_all_lecture_summaries,
    update_summary_image_pages,
    replace_lecture_figures,
    get_lecture_figures,
    get_lecture_exam_figures,
    get_quiz_progress,
    save_quiz_result,
    reset_quiz_progress,
    get_global_review_queue,
    get_subject_progress_overview,
    get_latest_generation_job,
    update_lecture_summary_content,
    get_additional_quiz_sets,
    add_additional_quiz_set,
    get_base_web_quiz,
    save_base_web_quiz,
    enqueue_generation_job,
    cancel_pending_generation_job,
    dismiss_failed_generation_job,
    get_generation_jobs,
    get_lecture_exam_files,
    enqueue_drive_sync_job,
    get_drive_sync_jobs,
)


# =========================================================
# 기본 설정
# =========================================================

st.set_page_config(
    page_title="MedStudy",
    page_icon="📚",
    layout="wide",
)


@st.cache_resource(show_spinner=False)
def ensure_parent_watchdog():
    """네이티브 앱(MedStudyApp.swift)이 강제 종료돼도 이 streamlit
    프로세스가 고아로 남아 포트를 계속 붙잡지 않도록 감시한다.

    `MedStudyApp.swift`의 `stopOwnedServer()`는 `applicationWillTerminate`
    안에서만 이 프로세스를 종료시키는데, 이 콜백은 정상 종료(Cmd+Q)에만
    불리고 강제 종료(Force Quit, SIGKILL)에는 전혀 불린다. 그러면 이
    streamlit 프로세스가 launchd 밑으로 고아가 되어 계속 살아남아 포트
    8504를 붙잡고, 다음에 앱을 다시 켜면 "포트를 쓸 수 없다"며 실패한다.
    `generation_worker.py`/`drive_sync_worker.py`가 이미 같은 방식으로
    자신의 부모(이 streamlit 프로세스)를 감시해 부모가 사라지면 스스로
    끝내고 있어서(`--parent-pid`), 같은 패턴을 한 단계 위(streamlit
    자신 → 네이티브 앱)에도 적용한다.
    """
    parent_pid_text = os.environ.get("MEDSTUDY_PARENT_PID")
    if not parent_pid_text:
        return False

    try:
        parent_pid = int(parent_pid_text)
    except ValueError:
        return False

    def watch():
        while True:
            sleep(3)
            try:
                os.kill(parent_pid, 0)
            except OSError:
                os._exit(0)

    threading.Thread(target=watch, daemon=True).start()
    return True


ensure_parent_watchdog()

st.markdown(
    """
    <style>
    div[data-testid="stVerticalBlockBorderWrapper"] h1 {
        font-size: 2rem;
        line-height: 1.3;
        margin-top: 0.25rem;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] h2 {
        font-size: 1.45rem;
        line-height: 1.4;
        margin-top: 2rem;
        padding-bottom: 0.35rem;
        border-bottom: 1px solid #d9dee7;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] h3 {
        font-size: 1.15rem;
        line-height: 1.45;
        margin-top: 1.4rem;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] p,
    div[data-testid="stVerticalBlockBorderWrapper"] li {
        font-size: 1rem;
        line-height: 1.75;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] table {
        font-size: 0.95rem;
        line-height: 1.55;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] code {
        font-family: "Apple SD Gothic Neo", sans-serif !important;
        font-size: inherit !important;
        color: inherit !important;
        background: transparent !important;
        padding: 0 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OCR_DATA_DIR = BASE_DIR / "ocr_data"

DATA_DIR.mkdir(
    exist_ok=True
)


@st.cache_data(ttl=30, show_spinner=False)
def get_cached_drive_semesters():
    """구글 드라이브 학기 폴더 탐색 결과를 잠깐 캐시한다.

    사이드바 코드도 탭과 마찬가지로 접혀 있든 펼쳐 있든 앱 어디를
    클릭해도 매 재실행마다 실행된다. `discover_google_drive_semesters`는
    `~/Library/CloudStorage`(구글 드라이브 파일 프로바이더가 관리하는
    경로)를 순회하는데, 이 경로는 로컬 디스크가 아니라 동기화 상태에
    따라 느려질 수 있어 간격 제한 없이 매번 부르면 아무 버튼을 눌러도
    이 지연을 기다리게 된다. 학기 폴더 구성은 자주 안 바뀌므로 30초
    지연은 무의미하다.
    """
    return discover_google_drive_semesters()


# max_entries가 없으면 정리본을 열람할 때마다 생성된 PDF 바이트가
# 세션이 끝날 때까지 메모리에 영원히 쌓인다(자연 만료 없음).
@st.cache_data(show_spinner=False, max_entries=20)
def build_cached_summary_pdf(
    summary_content,
    lecture_pages_json,
    lecture_figures_json,
    exam_figures_json,
    export_version,
):
    return create_summary_pdf(
        summary_content,
        json.loads(lecture_pages_json),
        json.loads(lecture_figures_json),
        json.loads(exam_figures_json),
    )


@st.cache_data(show_spinner=False, max_entries=20)
def build_cached_exam_pdf(
    exam_content,
    exam_figures_json,
    export_version,
):
    """정리본 본문과 분리된 `## 4. 기출문제 분석` 섹션만 PDF로 만든다."""
    return create_summary_pdf(
        exam_content,
        [],
        [],
        json.loads(exam_figures_json),
    )


def rows_with_image_revisions(rows):
    """이미지 파일 교체도 PDF 캐시 키에 반영한다."""
    result = []
    for row in rows:
        item = dict(row)
        image_path = Path(item.get("image_path") or "")
        try:
            stat = image_path.stat()
            item["_image_revision"] = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            item["_image_revision"] = "missing"
        result.append(item)
    return result


def build_library_summary_pdf(lecture_id):
    """모아보기에서 선택한 정리본 하나만 지연 생성한다."""
    saved_summary = get_lecture_summary(lecture_id)
    if not saved_summary:
        return b""
    return build_cached_summary_pdf(
        saved_summary["content"],
        json.dumps(
            [dict(page) for page in get_lecture_pages(lecture_id)],
            ensure_ascii=False,
            sort_keys=True,
        ),
        json.dumps(
            rows_with_image_revisions(get_lecture_figures(lecture_id)),
            ensure_ascii=False,
            sort_keys=True,
        ),
        json.dumps(
            rows_with_image_revisions(get_lecture_exam_figures(lecture_id)),
            ensure_ascii=False,
            sort_keys=True,
        ),
        PDF_EXPORT_VERSION,
    )


def update_saved_summary_title(
    summary_content,
    subject_name,
    lecture_title,
    lecture_no=None,
):
    """저장된 Markdown 정리본의 첫 제목만 새 강의 제목으로 바꾼다."""
    if not summary_content:
        return summary_content
    lecture_label = (
        f"{lecture_no}강 {lecture_title}" if lecture_no is not None and lecture_title
        else f"{lecture_no}강" if lecture_no is not None
        else lecture_title
    )
    heading = f"# {subject_name} - {lecture_label}"
    updated, count = re.subn(
        r"(?m)^#\s+[^\n]+$",
        heading,
        summary_content,
        count=1,
    )
    return updated if count else summary_content


def change_quiz_item_index(index_state_key, delta, item_count):
    """문제를 한 번에 하나씩 앞뒤로 넘긴다."""
    current = st.session_state.get(index_state_key, 0)
    st.session_state[index_state_key] = max(
        0, min(item_count - 1, current + delta)
    )


def submit_quiz_answer(lecture_id, item, choice_key, result_key):
    """선택지 버튼의 on_click 콜백으로 채점까지 한 번에 끝낸다.

    콜백은 화면이 다시 그려지기 전에 실행되므로, 버튼을 누른 바로 그
    재실행에서 이미 채점된 결과를 볼 수 있다. 예전에는 버튼 클릭 처리를
    본문 코드 안에서 하고 끝에 `st.rerun()`을 한 번 더 불러 재실행을
    두 번 하게 만들어, 클릭부터 채점 표시까지 화면이 한 박자 늦게
    느껴졌다. 클라우드 동기화도 매 답변마다 강제로 기다리지 않고(로컬
    저장은 이미 끝나 답을 잃지 않는다) 화면 위쪽에서 5초 간격으로 도는
    일반 동기화에 맡겨, 매번 걸리던 네트워크 왕복을 없앤다.
    """
    is_correct = choice_key == item["correct_choice"]
    saved_answer = f'{item["correct_choice"]}. {item["answer_text"]}'
    if item.get("explanation"):
        saved_answer += f' | 해설: {item["explanation"]}'

    save_quiz_result(
        lecture_id,
        item["question_key"],
        item["question"],
        saved_answer,
        is_correct,
    )
    st.session_state[result_key] = {
        "selected_choice": choice_key,
        "is_correct": is_correct,
    }


def refresh_cloud_quiz_progress(lecture_id, force=False):
    """화면 재실행마다 네트워크를 호출하지 않도록 간격을 제한한다.

    Streamlit의 `st.tabs()`는 시각적 전환일 뿐이라, 어떤 탭이 화면에
    보이든 이 함수가 속한 "복습 퀴즈" 탭의 코드는 매 재실행마다(= 앱
    어디를 클릭하든) 실행된다. 간격이 너무 짧으면 퀴즈 탭을 보고 있지
    않을 때도 버튼 하나 누를 때마다 Supabase 네트워크 왕복을 기다리게
    된다. 답 제출마다 강제 동기화하던 것은 이미 없앴으므로, 이 주기적
    동기화가 다른 기기와의 진행률을 맞추는 유일한 수단이다. 45초 정도는
    기기 간 동기화 체감에 문제없으면서 호출 빈도를 크게 줄여준다.
    """
    state_key = f"cloud_quiz_sync_time_{lecture_id}"
    now = monotonic()
    if not force and now - st.session_state.get(state_key, 0) < 45:
        return None
    result = try_sync_quiz_progress(lecture_id)
    st.session_state[state_key] = now
    return result


def load_lecture_quiz_items_by_key(lecture_id):
    """한 강의의 기본 문제 + 추가 문제를 모두 question_key로 찾아본다.

    '오늘의 복습·오답 노트' 화면은 여러 강의를 넘나들며 quiz_progress
    행만 먼저 조회하는데, 실제 문제 본문·선택지는 각 강의 정리본
    안에만 있어 강의별로 다시 구성해야 한다. 기본 문제를 객관식으로
    변환하기 전에는(get_base_web_quiz가 없을 때) 정리본 원문에서 바로
    추출하는 tab5의 규칙과 동일하게 맞춘다.
    """
    quiz_summary = get_lecture_summary(lecture_id)
    if not quiz_summary:
        return {}

    base_web_quiz = get_base_web_quiz(lecture_id)
    base_quiz_content = (
        base_web_quiz["content"] if base_web_quiz else quiz_summary["content"]
    )

    items = extract_review_quiz(base_quiz_content)
    for quiz_set in get_additional_quiz_sets(lecture_id):
        items.extend(extract_review_quiz(quiz_set["content"]))

    return {item["question_key"]: item for item in items}


@st.cache_resource(show_spinner=False)
def initialize_database():
    """Streamlit이 화면을 다시 그릴 때 DB 마이그레이션을 반복하지 않는다."""
    init_db()
    return True


# DB 초기화는 앱 프로세스당 한 번만 실행한다.
initialize_database()


# =========================================================
# 제목
# =========================================================

st.title("MedStudy")

st.caption(
    "AI 기반 의학 강의 학습 시스템"
)


# =========================================================
# 사이드바 - 아이패드 직접 접속 주소
# =========================================================


def _get_local_network_urls():
    port = st.get_option("server.port") or 8501
    urls = []

    hostname = socket.gethostname()
    if hostname:
        urls.append(f"http://{hostname}:{port}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        lan_ip = sock.getsockname()[0]
        urls.append(f"http://{lan_ip}:{port}")
    except OSError:
        pass
    finally:
        sock.close()

    return urls


with st.sidebar.expander("📶 아이패드에서 Mac에 직접 접속", expanded=False):
    st.caption(
        "Mac과 아이패드가 같은 Wi-Fi에 연결되어 있으면, "
        "클라우드 없이 아래 주소로 바로 접속할 수 있습니다."
    )
    for direct_url in _get_local_network_urls():
        st.code(direct_url, language=None)
    st.caption("Mac이 잠자거나 꺼져 있으면 접속할 수 없습니다.")


# =========================================================
# 사이드바 - 과목
# =========================================================

st.sidebar.title("과목")

if "app_view" not in st.session_state:
    st.session_state["app_view"] = "lecture"

if st.session_state["app_view"] in ("summaries", "review", "dashboard"):
    if st.sidebar.button(
        "← 강의 화면으로",
        use_container_width=True,
    ):
        st.session_state["app_view"] = "lecture"
        st.rerun()
else:
    if st.sidebar.button(
        "📚 정리본 모아보기",
        use_container_width=True,
    ):
        st.session_state["app_view"] = "summaries"
        st.rerun()
    if st.sidebar.button(
        "🔁 오늘의 복습 · 오답 노트",
        use_container_width=True,
    ):
        st.session_state["app_view"] = "review"
        st.rerun()
    if st.sidebar.button(
        "📊 학습 대시보드",
        use_container_width=True,
    ):
        st.session_state["app_view"] = "dashboard"
        st.rerun()

def _background_jobs_are_active():
    """대기·진행 중인 작업이 있을 때만 자동 새로고침을 켠다.

    이전에는 `run_every="2s"`가 고정이라 앱을 켜놓고 아무 작업도 없을
    때조차 매 2초마다 영원히 DB를 조회하고 화면을 다시 그렸다. 이
    끊임없는 재실행이 Python 프로세스와 WKWebView(Web Content) 양쪽
    메모리를 계속 불려서, 정작 할 일이 없을 때도 램 사용량이 줄지
    않는 원인이었다. 이제는 실제로 진행 중이거나 대기 중인 작업이 있을
    때만 2초 간격으로 폴링하고, 없으면 자동 새로고침을 아예 끈다
    (다른 위젯 조작으로 전체 스크립트가 다시 실행될 때 다시 판단한다).
    """
    sync_jobs = get_drive_sync_jobs(limit=3)
    if any(job["status"] in ("pending", "running") for job in sync_jobs):
        return True
    jobs = get_generation_jobs(limit=8)
    return any(job["status"] in ("pending", "running") for job in jobs)


@st.fragment(run_every="2s" if _background_jobs_are_active() else None)
def render_background_generation_status():
    # Streamlit AppTest는 실제 앱이 아니므로 외부 작업 프로세스를 만들지 않는다.
    # 테스트 샌드박스에서 생성된 작업자가 실제 대기열을 잡는 것을 방지한다.
    script_context = get_script_run_ctx(suppress_warning=True)
    if not (
        script_context
        and str(script_context.session_id).startswith("test session id")
    ):
        ensure_generation_worker()
        ensure_drive_sync_worker()
    sync_jobs = get_drive_sync_jobs(limit=3)
    active_sync_jobs = [
        job for job in sync_jobs if job["status"] in ("pending", "running")
    ]
    failed_sync_jobs = [job for job in sync_jobs if job["status"] == "failed"]
    completed_sync_jobs = [job for job in sync_jobs if job["status"] == "completed"]
    jobs = get_generation_jobs(limit=8)
    active_jobs = [job for job in jobs if job["status"] in ("pending", "running")]
    failed_jobs = [
        job for job in jobs
        if job["status"] == "failed"
        and not any(
            newer["lecture_id"] == job["lecture_id"]
            and newer["id"] > job["id"]
            and newer["status"] in (
                "pending", "running", "completed", "cancelled"
            )
            for newer in jobs
        )
    ]

    with st.expander(
        f"백그라운드 작업 {len(active_sync_jobs) + len(active_jobs)}개",
        expanded=bool(active_sync_jobs or active_jobs),
    ):
        if not active_sync_jobs and not active_jobs and not failed_jobs and not failed_sync_jobs:
            st.caption("현재 동기화·생성 작업이 없습니다.")

        for job in reversed(active_sync_jobs):
            st.caption(f'Google Drive · {job["message"]}')
            st.progress(float(job["progress"]))

        for job in failed_sync_jobs[:1]:
            st.error("Google Drive 동기화 실패")
            st.caption(job["error"] or "알 수 없는 오류")

        if completed_sync_jobs and not active_sync_jobs:
            result = json.loads(completed_sync_jobs[0]["result_json"] or "{}")
            st.success(
                "최근 동기화 완료 · "
                f'강의 추가 {result.get("created", 0)} · '
                f'강의 변경 {result.get("updated", 0)} · '
                f'기출 추가 {result.get("exam_created", 0)} · '
                f'기출 변경 {result.get("exam_updated", 0)} · '
                f'생성 대기열 {result.get("queued_count", 0)}'
            )

        for job in reversed(active_jobs):
            label = f'{job["subject_name"]} {job["lecture_no"]}강'
            if job["status"] == "pending":
                status_column, cancel_column = st.columns([3, 1])
                with status_column:
                    st.caption(f"{label} · 대기 중")
                with cancel_column:
                    if st.button(
                        "취소",
                        key=f'cancel_generation_{job["id"]}',
                        help="아직 시작하지 않은 생성 작업을 대기열에서 제거합니다.",
                    ):
                        if cancel_pending_generation_job(job["id"]):
                            st.rerun(scope="fragment")
                        else:
                            st.warning("이미 생성을 시작해 취소할 수 없습니다.")
            else:
                st.caption(f'{label} · {job["stage"]} · {job["message"]}')
                st.progress(float(job["progress"]))

        for job in failed_jobs[:3]:
            label = f'{job["subject_name"]} {job["lecture_no"]}강'
            st.error(f"{label} · 생성 실패")
            st.caption(job["error"] or "알 수 없는 오류")
            retry_column, dismiss_column = st.columns(2)
            with retry_column:
                if st.button("다시 시도", key=f'retry_generation_{job["id"]}'):
                    enqueue_generation_job(job["lecture_id"])
                    st.rerun(scope="fragment")
            with dismiss_column:
                if st.button("숨기기", key=f'dismiss_failure_{job["id"]}'):
                    dismiss_failed_generation_job(job["id"])
                    st.rerun(scope="fragment")

    recent_summaries = get_recent_lecture_summaries(limit=5)
    with st.expander("최근 생성된 정리본", expanded=False):
        if not recent_summaries:
            st.caption("아직 생성된 정리본이 없습니다.")
        for summary in recent_summaries:
            lecture_label = (
                f'{summary["subject_name"]} '
                f'{summary["lecture_no"]}강'
            )
            if summary["lecture_title"]:
                lecture_label += f' · {summary["lecture_title"]}'
            st.markdown(f"**{lecture_label}**")
            st.caption(
                "생성 시각 · "
                f'{format_korea_datetime(summary["created_at"])}'
            )


with st.sidebar:
    render_background_generation_status()


# =========================================================
# Google Drive 로컬 동기화
# =========================================================

with st.sidebar.expander("Google Drive 자료 동기화", expanded=False):
    drive_notice = st.session_state.pop("drive_sync_notice", None)
    if drive_notice:
        if drive_notice.get("errors"):
            st.warning(drive_notice["message"])
            for error_message in drive_notice["errors"]:
                st.caption(error_message)
        else:
            st.success(drive_notice["message"])

    discovered_semesters = get_cached_drive_semesters()
    semester_options = [str(path) for path in discovered_semesters]

    if semester_options:
        selected_semester_path = st.selectbox(
            "학기 폴더",
            semester_options,
            format_func=lambda value: Path(value).name,
            key="drive_semester_path",
        )
        st.caption("Mac에 연결된 Google Drive에서 자동으로 찾았습니다.")
    else:
        selected_semester_path = st.text_input(
            "Google Drive 학기 폴더 경로",
            key="manual_drive_semester_path",
        )
        st.caption("자동 탐색에 실패했습니다. 학기 폴더 경로를 입력해주세요.")

    if st.button(
        "새 자료 찾기",
        use_container_width=True,
        disabled=not bool(selected_semester_path),
        key="scan_google_drive",
    ):
        try:
            scanned_candidates = scan_semester_folder(selected_semester_path)
            actionable_candidates, scan_stats = filter_actionable_candidates(
                scanned_candidates,
                DATA_DIR,
            )
            st.session_state["drive_sync_candidates"] = actionable_candidates
            st.session_state["drive_sync_stats"] = scan_stats
            st.session_state["drive_sync_scanned_path"] = selected_semester_path
        except Exception as error:
            st.error(f"Google Drive 자료를 찾지 못했습니다: {error}")

    drive_candidates = st.session_state.get("drive_sync_candidates", [])
    drive_scan_stats = st.session_state.get("drive_sync_stats")
    if drive_candidates:
        ready_count = len(drive_candidates)
        st.write(f"새로 처리할 자료 {ready_count}개")
        with st.expander("발견한 자료 보기", expanded=False):
            for item in drive_candidates:
                if item.get("item_type") == "exam":
                    st.caption(
                        f'{item["subject"]} · 기출 · {item["file_name"]} · '
                        f'{item["change_reason"]}'
                    )
                else:
                    st.caption(
                        f'{item["subject"]} {item["lecture_no"]}강 · '
                        f'{item["change_reason"]}'
                    )

        auto_generate_after_sync = st.checkbox(
            "동기화 후 AI 정리본 자동 생성",
            value=True,
            key="auto_generate_after_drive_sync",
        )
        st.caption(
            "파일명이 n_ 형태로 시작하면 n강을 자동 생성합니다. "
            "같은 n_ 파일이 여러 개면 파일명 순서대로 모두 합칩니다. "
            "한쪽 자료만 있으면 먼저 등록하고, 강의록과 TXT 전사문이 "
            "갖춰진 뒤 AI 생성 대기열에 등록합니다."
        )

        if st.button(
            "확인된 자료 동기화",
            type="primary",
            use_container_width=True,
            disabled=(ready_count == 0),
            key="sync_google_drive_candidates",
        ):
            try:
                _, created = enqueue_drive_sync_job(
                    drive_candidates,
                    DATA_DIR,
                    auto_generate=auto_generate_after_sync,
                )
            except Exception as error:
                st.error(f"동기화 대기열 등록 중 오류가 발생했습니다: {error}")
            else:
                ensure_drive_sync_worker()
                notice_message = (
                    "Google Drive 동기화를 백그라운드 대기열에 추가했습니다."
                )
                st.session_state["drive_sync_notice"] = {
                    "message": notice_message,
                    "errors": [],
                }
                st.session_state.pop("drive_sync_candidates", None)
                st.session_state.pop("drive_sync_stats", None)
                st.rerun()
    elif drive_scan_stats is not None:
        st.success("새로 추가되거나 변경된 자료가 없습니다.")
        if drive_scan_stats["hidden_incomplete"]:
            st.caption(
                "자동 처리할 수 없는 자료 "
                f'{drive_scan_stats["hidden_incomplete"]}개는 자동 처리에서 제외했습니다.'
            )


# -------------------------
# 새 과목 추가
# -------------------------

with st.sidebar.expander(
    "＋ 새 과목",
    expanded=False,
):

    new_subject = st.text_input(
        "과목명",
        placeholder="예: 생화학",
        key="new_subject",
    )

    if st.button(
        "과목 추가",
        use_container_width=True,
    ):

        if not new_subject.strip():

            st.warning(
                "과목명을 입력해주세요."
            )

        else:

            success = add_subject(
                new_subject
            )

            if success:

                st.success(
                    "과목이 추가되었습니다."
                )

                st.rerun()

            else:

                st.warning(
                    "이미 존재하는 과목입니다."
                )


# =========================================================
# 과목 목록
# =========================================================

subjects = get_subjects()


if not subjects:

    st.info(
        "왼쪽에서 첫 과목을 만들어주세요."
    )

    st.stop()


subjects_by_id = {
    subject["id"]: subject
    for subject in subjects
}


# =========================================================
# 정리본 모아보기
# =========================================================

if st.session_state.get("app_view") == "summaries":
    st.header("정리본 모아보기")
    all_summaries = get_all_lecture_summaries()

    if not all_summaries:
        st.info("아직 생성된 정리본이 없습니다.")
        st.stop()

    subject_names = list(dict.fromkeys(
        summary["subject_name"] for summary in all_summaries
    ))
    selected_summary_subject = st.selectbox(
        "과목",
        ["전체 과목", *subject_names],
        key="summary_library_subject",
    )
    visible_summaries = [
        summary for summary in all_summaries
        if selected_summary_subject == "전체 과목"
        or summary["subject_name"] == selected_summary_subject
    ]

    st.caption(f"정리본 {len(visible_summaries)}개 · 최근 생성 순")

    for summary in visible_summaries:
        with st.container(border=True):
            info_column, date_column, open_column, download_column = st.columns(
                [5, 3, 1, 1.5]
            )
            with info_column:
                st.markdown(f'**{summary["subject_name"]}**')
                lecture_label = f'{summary["lecture_no"]}강'
                if summary["lecture_title"]:
                    lecture_label += f' · {summary["lecture_title"]}'
                st.write(lecture_label)
            with date_column:
                st.caption("생성 날짜")
                st.write(format_korea_datetime(summary["created_at"]))
            with open_column:
                if st.button(
                    "열기",
                    key=f'open_summary_{summary["lecture_id"]}',
                    use_container_width=True,
                ):
                    st.session_state["selected_subject"] = summary["subject_id"]
                    st.session_state["selected_lecture"] = summary["lecture_id"]
                    st.session_state["app_view"] = "lecture"
                    st.rerun()
            with download_column:
                st.download_button(
                    "PDF 다운로드",
                    data=(
                        lambda lecture_id=summary["lecture_id"]:
                        build_library_summary_pdf(lecture_id)
                    ),
                    file_name=(
                        f'{summary["subject_name"]}_'
                        f'{summary["lecture_no"]:02d}강_정리본.pdf'
                    ),
                    mime="application/pdf",
                    key=f'download_library_summary_{summary["lecture_id"]}',
                    on_click="ignore",
                    use_container_width=True,
                )

    st.stop()


# =========================================================
# 오늘의 복습 · 오답 노트 (모든 과목·강의 통합)
# =========================================================

if st.session_state.get("app_view") == "review":
    st.header("오늘의 복습 · 오답 노트")
    st.caption(
        "간격 반복(SM-2) 일정상 오늘 복습할 문제와, 과목을 넘나들며 "
        "한 번이라도 틀린 문제를 한 곳에 모아 보여줍니다."
    )

    progress_rows = get_global_review_queue()

    if not progress_rows:
        st.info("오늘 복습할 문제나 오답 기록이 없습니다.")
        st.stop()

    # 문제 본문·선택지는 각 강의 정리본에만 있으므로, 이 화면에 등장하는
    # 강의만 한 번씩 불러와 question_key로 다시 찾아 붙인다.
    lecture_ids = sorted({row["lecture_id"] for row in progress_rows})
    items_by_lecture = {
        lecture_id: load_lecture_quiz_items_by_key(lecture_id)
        for lecture_id in lecture_ids
    }

    queue_items = []
    for row in progress_rows:
        item = items_by_lecture.get(row["lecture_id"], {}).get(
            row["question_key"]
        )
        if not item:
            # 정리본을 다시 생성해 문제 내용/해시가 바뀐 경우 등은 건너뛴다.
            continue

        lecture_label = f'{row["lecture_no"]}강'
        if row["lecture_title"]:
            lecture_label += f' · {row["lecture_title"]}'

        queue_items.append({
            **item,
            "_lecture_id": row["lecture_id"],
            "_composite_key": f'{row["lecture_id"]}:{row["question_key"]}',
            "_subject_id": row["subject_id"],
            "_subject_name": row["subject_name"],
            "_lecture_label": lecture_label,
            "_due_date": row["due_date"],
            "_incorrect_count": row["incorrect_count"],
            "_correct_count": row["correct_count"],
        })

    items_by_composite = {
        item["_composite_key"]: item for item in queue_items
    }
    due_keys = [
        item["_composite_key"] for item in queue_items
        if is_due(item["_due_date"])
    ]
    wrong_keys = [
        item["_composite_key"] for item in queue_items
        if item["_incorrect_count"] >= 1
    ]

    metric1, metric2 = st.columns(2)
    metric1.metric("오늘 복습 대상", len(due_keys))
    metric2.metric("오답 기록 있는 문제", len(wrong_keys))

    filter_label = st.radio(
        "보기",
        ["오늘 복습", "오답 노트"],
        horizontal=True,
        key="global_review_filter",
    )

    index_state_key = "global_review_index"
    filter_track_key = "global_review_filter_track"
    filter_snapshot_key = "global_review_filter_snapshot"

    if st.session_state.get(filter_track_key) != filter_label:
        if filter_label == "오늘 복습":
            snapshot_keys = sorted(
                due_keys,
                key=lambda key: items_by_composite[key]["_due_date"],
            )
        else:
            snapshot_keys = wrong_keys

        st.session_state[filter_snapshot_key] = snapshot_keys
        st.session_state[index_state_key] = 0
        st.session_state[filter_track_key] = filter_label

    filtered_items = [
        items_by_composite[key]
        for key in st.session_state.get(filter_snapshot_key, [])
        if key in items_by_composite
    ]

    if not filtered_items:
        st.info("이 조건에 해당하는 문제가 없습니다.")
        st.stop()

    current_index = min(
        st.session_state.get(index_state_key, 0),
        len(filtered_items) - 1,
    )
    st.session_state[index_state_key] = current_index
    item = filtered_items[current_index]
    lecture_id = item["_lecture_id"]

    with st.container(border=True):
        nav_prev, nav_label, nav_next = st.columns([1, 2, 1])
        with nav_prev:
            st.button(
                "◀ 이전 문제",
                disabled=(current_index == 0),
                width="stretch",
                key="global_review_prev",
                on_click=change_quiz_item_index,
                args=(index_state_key, -1, len(filtered_items)),
            )
        with nav_label:
            st.markdown(
                "<p style='text-align:center; margin-top:0.45rem;'>"
                f"<b>문제 {current_index + 1} / {len(filtered_items)}</b>"
                "</p>",
                unsafe_allow_html=True,
            )
        with nav_next:
            st.button(
                "다음 문제 ▶",
                disabled=(current_index == len(filtered_items) - 1),
                width="stretch",
                key="global_review_next",
                on_click=change_quiz_item_index,
                args=(index_state_key, 1, len(filtered_items)),
            )

        badge_col, open_col = st.columns([4, 1])
        with badge_col:
            st.caption(f'{item["_subject_name"]} · {item["_lecture_label"]}')
        with open_col:
            if st.button(
                "강의로 이동",
                key=f'go_to_lecture_{item["_composite_key"]}',
                width="stretch",
            ):
                st.session_state["selected_subject"] = item["_subject_id"]
                st.session_state["selected_lecture"] = lecture_id
                st.session_state["app_view"] = "lecture"
                st.rerun()

        st.markdown(f'**Q{item["number"]}. {item["question"]}**')

        result_key = f'quiz_result_{lecture_id}_{item["question_key"]}'
        result = st.session_state.get(result_key)

        if result is None:
            for choice_key in ("A", "B", "C", "D"):
                st.button(
                    f'{choice_key}. {item["choices"][choice_key]}',
                    width="stretch",
                    key=f'global_choice_{item["_composite_key"]}_{choice_key}',
                    on_click=submit_quiz_answer,
                    args=(lecture_id, item, choice_key, result_key),
                )
        else:
            selected_choice = result["selected_choice"]
            for choice_key in ("A", "B", "C", "D"):
                choice_text = f'{choice_key}. {item["choices"][choice_key]}'
                if choice_key == item["correct_choice"]:
                    st.success(f"✅ {choice_text}")
                elif choice_key == selected_choice:
                    st.error(f"❌ {choice_text}")
                else:
                    st.write(choice_text)

            if result["is_correct"]:
                st.success("정답입니다.")
            else:
                st.error("오답입니다. 복습 일정을 다시 조정했습니다.")

            if item.get("explanation"):
                st.caption(f'해설 · {item["explanation"]}')

            if st.button(
                "다시 풀기",
                key=f'global_retry_{item["_composite_key"]}',
            ):
                del st.session_state[result_key]
                st.rerun()

        due_caption = (
            f' · 다음 복습 예정일 {item["_due_date"]}'
            if item["_due_date"] else ""
        )
        st.caption(
            f'맞힌 횟수 {item["_correct_count"]}회 · '
            f'틀린 횟수 {item["_incorrect_count"]}회{due_caption}'
        )

    st.stop()


# =========================================================
# 학습 대시보드 (과목별 정리 진도 · 퀴즈 성취도)
# =========================================================

if st.session_state.get("app_view") == "dashboard":
    st.header("학습 대시보드")
    st.caption(
        "성취도는 한 번이라도 풀어본 문제 기준입니다. "
        "성취도가 낮은 과목이 위로 오도록 정렬됩니다."
    )

    overview_rows = get_subject_progress_overview()

    if not overview_rows:
        st.info("등록된 과목이 없습니다.")
        st.stop()

    def _mastery_rate(row):
        if row["answered_count"] == 0:
            return -1.0  # 아직 안 푼 과목은 맨 위로 올려 눈에 띄게 한다.
        return row["mastered_count"] / row["answered_count"]

    total_lectures = sum(row["lecture_count"] for row in overview_rows)
    total_summarized = sum(row["summarized_count"] for row in overview_rows)
    total_answered = sum(row["answered_count"] for row in overview_rows)
    total_mastered = sum(row["mastered_count"] for row in overview_rows)
    total_due = sum(row["due_count"] for row in overview_rows)

    metric1, metric2, metric3, metric4 = st.columns(4)
    metric1.metric(
        "정리본 생성",
        f"{total_summarized} / {total_lectures}강",
    )
    metric2.metric(
        "전체 학습 성취도",
        f"{(total_mastered / total_answered * 100):.0f}%"
        if total_answered else "-",
    )
    metric3.metric("오늘 복습 대상", total_due)
    metric4.metric("풀어본 문제 수", total_answered)

    st.divider()

    for row in sorted(overview_rows, key=_mastery_rate):
        with st.container(border=True):
            title_col, open_col = st.columns([4, 1])
            with title_col:
                st.markdown(f'**{row["subject_name"]}**')
            with open_col:
                if st.button(
                    "과목 열기",
                    key=f'dashboard_open_subject_{row["subject_id"]}',
                    width="stretch",
                ):
                    st.session_state["selected_subject"] = row["subject_id"]
                    st.session_state["app_view"] = "lecture"
                    st.rerun()

            st.caption(
                f'강의 {row["summarized_count"]} / {row["lecture_count"]}개 '
                "정리 완료"
            )
            if row["lecture_count"]:
                st.progress(row["summarized_count"] / row["lecture_count"])

            if row["answered_count"] == 0:
                st.caption("아직 푼 문제가 없습니다.")
            else:
                mastery_rate = row["mastered_count"] / row["answered_count"]
                st.caption(
                    f'퀴즈 성취도 {mastery_rate * 100:.0f}% '
                    f'(학습 완료 {row["mastered_count"]} · '
                    f'복습 필요 {row["review_count"]} · '
                    f'오늘 복습 {row["due_count"]} · '
                    f'푼 문제 {row["answered_count"]}개)'
                )
                st.progress(mastery_rate)

    st.stop()


selected_subject_id = (
    st.session_state.get(
        "selected_subject"
    )
)


if selected_subject_id not in subjects_by_id:
    selected_subject_id = subjects[0]["id"]

    st.session_state[
        "selected_subject"
    ] = selected_subject_id


for subject in subjects:

    if st.sidebar.button(
        subject["name"],
        key=f'subject_{subject["id"]}',
        type=(
            "primary"
            if subject["id"] == selected_subject_id
            else "secondary"
        ),
        use_container_width=True,
    ):

        if subject["id"] != selected_subject_id:
            st.session_state[
                "selected_subject"
            ] = subject["id"]

            st.session_state.pop(
                "selected_lecture",
                None,
            )

            st.session_state.pop(
                "confirm_delete",
                None,
            )

            st.rerun()


selected_subject = (
    subjects_by_id[
        selected_subject_id
    ]
)


# =========================================================
# 강의 목록
# =========================================================

lectures = get_lectures(
    selected_subject["id"]
)


st.sidebar.divider()

st.sidebar.subheader(
    selected_subject["name"]
)


# -------------------------
# 강의 버튼
# -------------------------

for lecture in lectures:

    if lecture["title"]:

        label = (
            f'{lecture["lecture_no"]}강 '
            f'{lecture["title"]}'
        )

    else:

        label = (
            f'{lecture["lecture_no"]}강'
        )

    if st.sidebar.button(
        label,
        key=f'lecture_{lecture["id"]}',
        use_container_width=True,
    ):

        st.session_state[
            "selected_lecture"
        ] = lecture["id"]

        # 삭제 확인 상태 초기화
        st.session_state.pop(
            "confirm_delete",
            None,
        )

        st.session_state.pop(
            "editing_lecture_title",
            None,
        )

        st.rerun()


# =========================================================
# 강의 추가
# =========================================================

with st.sidebar.expander(
    "＋ 강의 추가",
    expanded=False,
):

    lecture_no = st.number_input(
        "강의 번호",
        min_value=1,
        step=1,
    )

    lecture_title = st.text_input(
        "강의 제목",
        placeholder="예: 세포막과 막수송",
    )

    if st.button(
        "강의 생성",
        use_container_width=True,
    ):

        success = add_lecture(
            selected_subject["id"],
            int(lecture_no),
            lecture_title,
        )

        if success:

            st.success(
                "강의가 추가되었습니다."
            )

            st.rerun()

        else:

            st.warning(
                f"{int(lecture_no)}강이 이미 존재합니다."
            )


# =========================================================
# 메인 화면
# =========================================================

st.header(
    selected_subject["name"]
)


# -------------------------
# 강의가 없는 경우
# -------------------------

if not lectures:

    st.write(
        "아직 등록된 강의가 없습니다."
    )

    st.write(
        "왼쪽의 **＋ 강의 추가**를 눌러 "
        "첫 강의를 만들어주세요."
    )

    st.stop()


# =========================================================
# 선택된 강의 결정
# =========================================================

saved_lecture_id = (
    st.session_state.get(
        "selected_lecture"
    )
)


selected_lecture = None


if saved_lecture_id:

    for lecture in lectures:

        if (
            lecture["id"]
            == saved_lecture_id
        ):

            selected_lecture = lecture

            break


# 선택된 강의가 없으면
# 첫 번째 강의 자동 선택
if selected_lecture is None:

    selected_lecture = lectures[0]

    st.session_state[
        "selected_lecture"
    ] = selected_lecture["id"]


# =========================================================
# 현재 강의 제목 + 삭제
# =========================================================

lecture_title = (
    selected_lecture["title"]
)


title_col, edit_col, delete_col = st.columns(
    [5, 1, 1]
)


with title_col:

    if lecture_title:

        st.subheader(
            f'{selected_lecture["lecture_no"]}강 '
            f'{lecture_title}'
        )

    else:

        st.subheader(
            f'{selected_lecture["lecture_no"]}강'
        )


with edit_col:

    if st.button(
        "제목 수정",
        use_container_width=True,
        key=f'edit_lecture_title_{selected_lecture["id"]}',
    ):

        st.session_state["editing_lecture_title"] = selected_lecture["id"]


with delete_col:

    if st.button(
        "강의 삭제",
        use_container_width=True,
    ):

        st.session_state[
            "confirm_delete"
        ] = True


if st.session_state.get("editing_lecture_title") == selected_lecture["id"]:

    with st.form(
        f'lecture_title_form_{selected_lecture["id"]}',
        clear_on_submit=False,
    ):

        edited_title = st.text_input(
            "새 강의 제목",
            value=lecture_title or "",
            max_chars=150,
        )

        save_col, cancel_edit_col = st.columns(2)

        with save_col:
            save_title = st.form_submit_button(
                "제목 저장",
                type="primary",
                use_container_width=True,
            )

        with cancel_edit_col:
            cancel_title_edit = st.form_submit_button(
                "취소",
                use_container_width=True,
            )

    if save_title:
        cleaned_title = edited_title.strip()
        if not cleaned_title:
            st.warning("강의 제목을 입력해주세요.")
        else:
            update_lecture_title(selected_lecture["id"], cleaned_title)

            # 이미 생성된 정리본도 다시 AI를 호출하지 않고 첫 제목만 갱신한다.
            current_summary = get_lecture_summary(selected_lecture["id"])
            if current_summary:
                updated_summary = update_saved_summary_title(
                    current_summary["content"],
                    selected_subject["name"],
                    cleaned_title,
                    selected_lecture["lecture_no"],
                )
                if updated_summary != current_summary["content"]:
                    update_lecture_summary_content(
                        selected_lecture["id"],
                        updated_summary,
                        current_summary["model"],
                    )

            # 클라우드 학습 앱이 설정된 경우 바뀐 제목도 즉시 반영한다.
            try:
                try_sync_lecture_to_cloud(selected_lecture["id"])
            except Exception as cloud_error:
                st.warning(
                    "제목은 저장했지만 클라우드 동기화에 실패했습니다: "
                    f"{cloud_error}"
                )

            st.session_state.pop("editing_lecture_title", None)
            st.success("강의 제목을 수정했습니다.")
            st.rerun()

    if cancel_title_edit:
        st.session_state.pop("editing_lecture_title", None)
        st.rerun()


# =========================================================
# 강의 삭제 확인
# =========================================================

if st.session_state.get(
    "confirm_delete"
):

    st.warning(
        "정말 이 강의를 삭제하시겠습니까? "
        "업로드된 강의 자료도 함께 삭제됩니다."
    )

    confirm_col1, confirm_col2 = (
        st.columns(2)
    )


    with confirm_col1:

        if st.button(
            "삭제 확인",
            type="primary",
            use_container_width=True,
        ):

            # 해당 강의 폴더
            lecture_folder = (
                DATA_DIR
                / selected_subject["name"]
                / f'{selected_lecture["lecture_no"]:02d}'
            )


            # 실제 파일 삭제
            if lecture_folder.exists():

                shutil.rmtree(
                    lecture_folder
                )


            # DB 삭제
            delete_lecture(
                selected_lecture["id"]
            )


            # 선택 상태 초기화
            st.session_state.pop(
                "selected_lecture",
                None,
            )

            st.session_state.pop(
                "confirm_delete",
                None,
            )

            st.session_state.pop(
                "editing_lecture_title",
                None,
            )


            st.rerun()


    with confirm_col2:

        if st.button(
            "취소",
            use_container_width=True,
        ):

            st.session_state.pop(
                "confirm_delete",
                None,
            )

            st.rerun()


st.divider()


# =========================================================
# 강의 탭
# =========================================================

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    [
        "강의록",
        "전사문",
        "AI 정리본",
        "기출 분석",
        "복습 퀴즈",
    ]
)


# =========================================================
# 강의록 탭
# =========================================================

with tab1:

    st.subheader(
        "강의록"
    )


    # -------------------------
    # 기존 PDF 확인
    # -------------------------

    pdf_path = (
        selected_lecture["pdf_path"]
    )


    if pdf_path:

        saved_pdf = Path(
            pdf_path
        )


        if saved_pdf.exists():

            st.success(
                "강의록이 등록되어 있습니다."
            )

            st.write(
                f"파일: `{saved_pdf.name}`"
            )


            # 앱 안에서 PDF 미리보기 (기본은 꺼둔다). Streamlit 탭은
            # 화면에 안 보여도 코드가 매 재실행마다(= 앱 어디를 클릭해도)
            # 실행되므로, 미리보기를 항상 그려두면 클릭할 때마다 이
            # 강의록 PDF 전체(수 MB~수십 MB)를 매번 다시 읽어 화면에
            # 내려보내게 된다. 토글을 켰을 때만 읽고 그린다.
            if st.toggle(
                "PDF 미리보기 표시",
                key=f'show_pdf_preview_{selected_lecture["id"]}',
            ):
                st.pdf(
                    saved_pdf.read_bytes(),
                    height=800,
                    key=f'pdf_preview_{selected_lecture["id"]}',
                )


            # PDF 다운로드
            with open(
                saved_pdf,
                "rb",
            ) as pdf_file:

                st.download_button(
                    label="PDF 열기 / 다운로드",
                    data=pdf_file,
                    file_name=saved_pdf.name,
                    mime="application/pdf",
                )


            lecture_pages = get_lecture_pages(
                selected_lecture["id"]
            )

            extract_label = (
                "페이지별 텍스트 다시 추출"
                if lecture_pages
                else "페이지별 텍스트 추출"
            )

            if st.button(
                extract_label,
                use_container_width=True,
                key=f'extract_pdf_{selected_lecture["id"]}',
            ):
                try:
                    page_texts = (
                        extract_pdf_page_texts(
                            saved_pdf
                        )
                    )

                    replace_lecture_pages(
                        selected_lecture["id"],
                        page_texts,
                    )

                except Exception as error:
                    st.error(
                        "PDF 텍스트 추출 중 오류가 "
                        f"발생했습니다: {error}"
                    )
                else:
                    st.success(
                        f"{len(page_texts)}페이지의 "
                        "텍스트를 추출했습니다."
                    )

                    st.rerun()


            if lecture_pages:
                pages_with_text = sum(
                    1
                    for page in lecture_pages
                    if page["text"].strip()
                )

                st.caption(
                    f"총 {len(lecture_pages)}페이지 중 "
                    f"{pages_with_text}페이지에서 "
                    "텍스트를 찾았습니다."
                )

                pages_with_ocr = sum(
                    1
                    for page in lecture_pages
                    if page["ocr_text"].strip()
                )

                pages_with_images = sum(
                    1
                    for page in lecture_pages
                    if page["image_path"]
                    and Path(page["image_path"]).exists()
                )

                st.caption(
                    f"OCR 완료 {pages_with_ocr}페이지 · "
                    f"페이지 이미지 {pages_with_images}장"
                )

                if st.button(
                    "페이지 이미지 생성 + 한글 OCR",
                    use_container_width=True,
                    key=(
                        f'process_pdf_ocr_'
                        f'{selected_lecture["id"]}'
                    ),
                ):
                    pages_dir = (
                        saved_pdf.parent
                        / "pages"
                    )
                    progress_bar = st.progress(0)
                    progress_text = st.empty()

                    def update_ocr_progress(
                        current_page,
                        total_pages,
                    ):
                        progress_bar.progress(
                            current_page / total_pages
                        )
                        progress_text.caption(
                            f"{current_page}/{total_pages}페이지 "
                            "처리 중"
                        )

                    try:
                        processed_pages = (
                            process_pdf_pages(
                                saved_pdf,
                                pages_dir,
                                OCR_DATA_DIR,
                                progress_callback=(
                                    update_ocr_progress
                                ),
                            )
                        )

                        replace_lecture_pages(
                            selected_lecture["id"],
                            processed_pages,
                        )

                    except Exception as error:
                        st.error(
                            "페이지 이미지/OCR 처리 중 "
                            f"오류가 발생했습니다: {error}"
                        )
                    else:
                        st.success(
                            f"{len(processed_pages)}페이지의 "
                            "이미지와 OCR 텍스트를 저장했습니다."
                        )
                        st.rerun()

                with st.expander(
                    "추출된 페이지별 텍스트 보기"
                ):
                    page_numbers = [
                        page["page_no"]
                        for page in lecture_pages
                    ]

                    selected_page_no = st.selectbox(
                        "페이지",
                        options=page_numbers,
                        format_func=lambda page_no: (
                            f"{page_no}페이지"
                        ),
                        key=(
                            f'extracted_page_'
                            f'{selected_lecture["id"]}'
                        ),
                    )

                    selected_page = next(
                        page
                        for page in lecture_pages
                        if page["page_no"]
                        == selected_page_no
                    )

                    page_text = (
                        selected_page["text"]
                        or "추출된 텍스트가 없습니다."
                    )

                    st.text_area(
                        "추출 결과",
                        value=page_text,
                        height=300,
                        disabled=True,
                        key=(
                            f'extracted_text_'
                            f'{selected_lecture["id"]}_'
                            f'{selected_page_no}'
                        ),
                    )

                    selected_image_path = (
                        selected_page["image_path"]
                    )

                    if (
                        selected_image_path
                        and Path(selected_image_path).exists()
                    ):
                        st.image(
                            selected_image_path,
                            caption=(
                                f"PDF {selected_page_no}페이지"
                            ),
                            use_container_width=True,
                        )

                    if selected_page["ocr_text"].strip():
                        st.text_area(
                            "이미지 OCR 보완 텍스트",
                            value=selected_page["ocr_text"],
                            height=300,
                            disabled=True,
                            key=(
                                f'ocr_text_'
                                f'{selected_lecture["id"]}_'
                                f'{selected_page_no}'
                            ),
                        )


        else:

            st.warning(
                "DB에는 강의록 정보가 있지만 "
                "실제 PDF 파일을 찾을 수 없습니다."
            )


    else:

        st.info(
            "아직 강의록이 업로드되지 않았습니다."
        )


    st.divider()


    # -------------------------
    # PDF 업로드
    # -------------------------

    uploaded_pdf = st.file_uploader(
        "강의록 PDF 업로드",
        type=["pdf"],
        key=f'pdf_{selected_lecture["id"]}',
    )


    if uploaded_pdf is not None:

        if st.button(
            "PDF 저장",
            type="primary",
            use_container_width=True,
            key=f'save_pdf_{selected_lecture["id"]}',
        ):

            # 과목 폴더
            subject_folder = (
                DATA_DIR
                / selected_subject["name"]
            )


            # 강의 폴더
            lecture_folder = (
                subject_folder
                / f'{selected_lecture["lecture_no"]:02d}'
            )


            lecture_folder.mkdir(
                parents=True,
                exist_ok=True,
            )


            # PDF 저장 경로
            pdf_path = (
                lecture_folder
                / "lecture.pdf"
            )


            # 실제 PDF 저장
            with open(
                pdf_path,
                "wb",
            ) as f:

                f.write(
                    uploaded_pdf.getbuffer()
                )


            # DB에 경로 저장
            update_lecture_pdf(
                selected_lecture["id"],
                str(pdf_path),
            )


            # 페이지별 텍스트 자동 추출
            try:
                page_texts = (
                    extract_pdf_page_texts(
                        pdf_path
                    )
                )

                replace_lecture_pages(
                    selected_lecture["id"],
                    page_texts,
                )

            except Exception as error:
                st.warning(
                    "PDF는 저장했지만 텍스트를 "
                    "추출하지 못했습니다: "
                    f"{error}"
                )


            st.success(
                "강의록이 저장되었습니다."
            )

            st.rerun()


# =========================================================
# 전사문 탭
# =========================================================

with tab2:

    st.subheader(
        "전사문"
    )

    transcript_path = (
        selected_lecture["transcript_path"]
    )

    if transcript_path:
        saved_transcript = Path(
            transcript_path
        )

        if saved_transcript.exists():
            st.success(
                "전사문이 등록되어 있습니다."
            )

            transcript_text = (
                saved_transcript.read_text(
                    encoding="utf-8-sig"
                )
            )

            st.text_area(
                "전사문 내용",
                value=transcript_text,
                height=350,
                disabled=True,
            )

            st.download_button(
                label="전사문 다운로드",
                data=transcript_text,
                file_name=saved_transcript.name,
                mime="text/plain",
            )

            st.divider()
            st.markdown("**강의록 기반 AI 교정본**")

            corrected_transcript_path = (
                selected_lecture["corrected_transcript_path"]
            )
            corrected_text = ""

            if corrected_transcript_path:
                saved_correction = Path(corrected_transcript_path)
                if saved_correction.exists():
                    corrected_text = saved_correction.read_text(
                        encoding="utf-8-sig"
                    )

            if corrected_text:
                st.success(
                    "AI 교정본이 있습니다. 아래에서 직접 고쳐 저장할 수 있습니다."
                )
                edited_correction = st.text_area(
                    "AI 교정본 내용",
                    value=corrected_text,
                    height=500,
                    key=f'corrected_transcript_{selected_lecture["id"]}',
                )

                save_col, download_col = st.columns(2)
                with save_col:
                    if st.button(
                        "교정본 수정 내용 저장",
                        use_container_width=True,
                        key=f'save_correction_{selected_lecture["id"]}',
                    ):
                        saved_correction.write_text(
                            edited_correction,
                            encoding="utf-8",
                        )
                        st.success("교정본을 저장했습니다.")
                        st.rerun()

                with download_col:
                    st.download_button(
                        "AI 교정본 다운로드",
                        data=edited_correction,
                        file_name="corrected_transcript.txt",
                        mime="text/plain",
                        use_container_width=True,
                        key=f'download_correction_{selected_lecture["id"]}',
                    )
            else:
                st.info("아직 AI 교정본이 없습니다.")

            lecture_pages_for_correction = get_lecture_pages(
                selected_lecture["id"]
            )
            codex_path_for_correction = find_codex_cli()

            st.caption(
                "아래 버튼을 누르면 원본 전사문과 강의록 추출 텍스트가 "
                "ChatGPT 구독으로 연결된 Codex에 전송되어 교정됩니다. "
                "원본 전사문은 변경하지 않습니다."
            )

            if not lecture_pages_for_correction:
                st.warning(
                    "먼저 강의록 탭에서 PDF 텍스트를 추출해주세요."
                )
            elif not codex_path_for_correction:
                st.warning("Codex CLI를 찾지 못했습니다.")
            elif st.button(
                "강의록 기반으로 전사문 AI 교정",
                type="primary",
                use_container_width=True,
                key=f'correct_transcript_{selected_lecture["id"]}',
            ):
                progress_bar = st.progress(0)
                progress_text = st.empty()

                def update_correction_progress(current, total):
                    progress_bar.progress((current - 1) / total)
                    progress_text.caption(
                        f"전사문 {current}/{total}구간 교정 중"
                    )

                try:
                    logged_in, login_message = get_codex_login_status(
                        codex_path_for_correction
                    )
                    if not logged_in:
                        raise RuntimeError(
                            "Codex 로그인이 필요합니다. "
                            f"{login_message}"
                        )

                    corrected_text = correct_transcript_with_codex(
                        codex_path_for_correction,
                        transcript_text,
                        lecture_pages_for_correction,
                        progress_callback=update_correction_progress,
                    )
                    correction_path = (
                        saved_transcript.parent
                        / "corrected_transcript.txt"
                    )
                    correction_path.write_text(
                        corrected_text,
                        encoding="utf-8",
                    )
                    update_corrected_transcript(
                        selected_lecture["id"],
                        str(correction_path),
                    )
                    progress_bar.progress(1.0)
                except Exception as error:
                    st.error(
                        "전사문 교정 중 오류가 발생했습니다: "
                        f"{error}"
                    )
                else:
                    st.success(
                        "원본은 보존하고 AI 교정본을 별도로 저장했습니다."
                    )
                    st.rerun()

        else:
            st.warning(
                "DB에는 전사문 정보가 있지만 "
                "실제 TXT 파일을 찾을 수 없습니다."
            )

    else:
        st.info(
            "아직 전사문이 업로드되지 않았습니다."
        )

    st.divider()
    st.caption("AI로 변환해 둔 원본 전사문을 TXT 형식으로 등록해주세요.")

    uploaded_transcript = st.file_uploader(
        "전사문 TXT 업로드",
        type=["txt"],
        key=f'transcript_{selected_lecture["id"]}',
    )

    if uploaded_transcript is not None:
        if st.button(
            "전사문 저장",
            type="primary",
            use_container_width=True,
            key=f'save_transcript_{selected_lecture["id"]}',
        ):
            try:
                transcript_text = (
                    uploaded_transcript
                    .getvalue()
                    .decode("utf-8-sig")
                )
            except UnicodeDecodeError:
                st.error(
                    "UTF-8 형식의 TXT 파일만 "
                    "업로드할 수 있습니다."
                )
            else:
                lecture_folder = (
                    DATA_DIR
                    / selected_subject["name"]
                    / f'{selected_lecture["lecture_no"]:02d}'
                )

                lecture_folder.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                transcript_path = (
                    lecture_folder
                    / "transcript.txt"
                )

                transcript_path.write_text(
                    transcript_text,
                    encoding="utf-8",
                )

                update_lecture_transcript(
                    selected_lecture["id"],
                    str(transcript_path),
                )
                update_corrected_transcript(
                    selected_lecture["id"],
                    None,
                )

                st.success(
                    "전사문이 저장되었습니다."
                )

                st.rerun()


# =========================================================
# AI 정리본 탭
# =========================================================

with tab3:

    st.subheader(
        "AI 정리본"
    )

    latest_generation_job = get_latest_generation_job(selected_lecture["id"])
    if (
        latest_generation_job
        and latest_generation_job["status"] == "completed"
        and "⚠" in (latest_generation_job["message"] or "")
    ):
        st.warning(
            "정리본은 만들어졌지만 일부 부가 단계가 조용히 건너뛰어졌습니다: "
            f'{latest_generation_job["message"].split("⚠", 1)[1].strip()}\n\n'
            "필요하면 아래에서 다시 생성해보세요."
        )

    with st.expander(
        "강의록 + 전사문으로 전체 자동 생성",
        expanded=(not selected_lecture["pdf_path"]),
    ):
        st.write(
            "PDF와 TXT 전사문을 선택한 뒤 한 번만 실행하면 OCR, 전사문 교정, "
            "핵심 그림 크롭과 AI 정리본 생성을 순서대로 완료합니다."
        )
        auto_pdf = st.file_uploader(
            "원본 강의록 PDF",
            type=["pdf"],
            key=f'auto_pdf_{selected_lecture["id"]}',
        )
        auto_transcript = st.file_uploader(
            "원본 전사문 TXT",
            type=["txt"],
            key=f'auto_transcript_{selected_lecture["id"]}',
        )
        st.caption(
            "실행하면 두 파일의 내용과 강의록 페이지 이미지가 "
            "ChatGPT 구독으로 연결된 Codex에 전송되어 분석됩니다. "
            "원본 파일은 그대로 보관됩니다."
        )

        if st.button(
            "자료 저장 + 백그라운드 생성 시작",
            type="primary",
            use_container_width=True,
            disabled=(auto_pdf is None or auto_transcript is None),
            key=f'run_full_automation_{selected_lecture["id"]}',
        ):
            try:
                lecture_folder = (
                    DATA_DIR
                    / selected_subject["name"]
                    / f'{selected_lecture["lecture_no"]:02d}'
                )
                lecture_folder.mkdir(parents=True, exist_ok=True)
                automatic_pdf_path = lecture_folder / "lecture.pdf"
                automatic_pdf_path.write_bytes(auto_pdf.getvalue())
                update_lecture_pdf(
                    selected_lecture["id"],
                    str(automatic_pdf_path),
                )
                try:
                    original_transcript = auto_transcript.getvalue().decode("utf-8-sig")
                except UnicodeDecodeError as error:
                    raise ValueError("UTF-8 형식의 TXT 전사문만 업로드할 수 있습니다.") from error
                automatic_transcript_path = lecture_folder / "transcript.txt"
                automatic_transcript_path.write_text(original_transcript, encoding="utf-8")
                update_lecture_transcript(selected_lecture["id"], str(automatic_transcript_path))
                update_corrected_transcript(
                    selected_lecture["id"],
                    None,
                )
                (lecture_folder / "generation_state.txt").write_text(
                    "pending",
                    encoding="utf-8",
                )
                _, job_created = enqueue_generation_job(
                    selected_lecture["id"]
                )
                ensure_generation_worker()
            except Exception as error:
                st.error(
                    "백그라운드 생성 요청 중 오류가 발생했습니다: "
                    f"{error}"
                )
            else:
                st.success(
                    "자료를 저장하고 백그라운드 생성 대기열에 "
                    + ("등록했습니다." if job_created else "이미 등록되어 있습니다.")
                )
                st.rerun()

    lecture_pages = get_lecture_pages(
        selected_lecture["id"]
    )
    lecture_figures = get_lecture_figures(
        selected_lecture["id"]
    )
    lecture_exam_figures = get_lecture_exam_figures(
        selected_lecture["id"]
    )

    saved_summary = get_lecture_summary(
        selected_lecture["id"]
    )

    # 동기화나 이전 생성 작업으로 정리본 첫 제목이 오래된 경우에도,
    # 현재 강의 제목과 PDF 제목이 항상 일치하도록 보정한다.
    if saved_summary:
        synced_summary_title = update_saved_summary_title(
            saved_summary["content"],
            selected_subject["name"],
            selected_lecture["title"] or f'{selected_lecture["lecture_no"]}강',
            selected_lecture["lecture_no"],
        )
        if synced_summary_title != saved_summary["content"]:
            update_lecture_summary_content(
                selected_lecture["id"],
                synced_summary_title,
                saved_summary["model"],
            )
            saved_summary = get_lecture_summary(selected_lecture["id"])

    # 기출이 많은 강의는 "## 4. 기출문제 분석" 섹션이 정리본 대부분을
    # 차지해 핵심 개념·강조 내용이 상대적으로 빈약해 보인다. 이 섹션은
    # 별도의 "기출 분석" 탭·PDF로 옮기고, 여기서는 본문만 보여준다.
    main_summary_content, exam_summary_content = (
        split_summary_sections(saved_summary["content"])
        if saved_summary
        else ("", "")
    )

    if saved_summary:
        with st.container(border=True):
            st.caption(
                f'생성 방식: {saved_summary["model"]} · '
                "생성 시각: "
                f'{format_korea_datetime(saved_summary["created_at"])}'
            )

            if "[[EXAM_HIGHLIGHT:" not in saved_summary["content"]:
                with st.expander(
                    "기존 정리본에 기출 하이라이트 적용",
                    expanded=False,
                ):
                    st.caption(
                        "기존 정리본의 1-4번 섹션이 ChatGPT 구독으로 연결된 "
                        "Codex에 전송됩니다. AI가 기출 분석과 직접 일치하는 "
                        "기존 문장만 고르고, 원문과 정확히 일치하는 구절에만 "
                        "#D287FF 하이라이트를 적용합니다."
                    )

                    if st.button(
                        "기출 하이라이트 분석 및 적용",
                        type="primary",
                        width="stretch",
                        key=f'apply_exam_highlight_{selected_lecture["id"]}',
                    ):
                        highlight_status = st.empty()
                        highlight_status.info(
                            "기출 분석과 앞쪽 요약 내용을 대조하고 있습니다."
                        )

                        try:
                            highlight_codex_path = find_codex_cli()
                            if not highlight_codex_path:
                                raise RuntimeError("Codex CLI를 찾지 못했습니다.")

                            logged_in, login_message = get_codex_login_status(
                                highlight_codex_path
                            )
                            if not logged_in:
                                raise RuntimeError(
                                    "Codex 로그인이 필요합니다. "
                                    f"{login_message}"
                                )

                            highlighted_content, highlight_count = (
                                highlight_summary_with_codex(
                                    highlight_codex_path,
                                    saved_summary["content"],
                                )
                            )
                            if highlight_count == 0:
                                raise RuntimeError(
                                    "기출 근거와 정확히 일치하는 앞쪽 문장을 "
                                    "찾지 못했습니다."
                                )

                            update_lecture_summary_content(
                                selected_lecture["id"],
                                highlighted_content,
                                saved_summary["model"],
                            )
                        except Exception as error:
                            highlight_status.empty()
                            st.error(
                                "기출 하이라이트 적용 중 오류가 발생했습니다: "
                                f"{error}"
                            )
                        else:
                            highlight_status.success(
                                f"기출 관련 구절 {highlight_count}개를 표시했습니다."
                            )
                            st.rerun()

            try:
                summary_pdf = build_cached_summary_pdf(
                    main_summary_content,
                    json.dumps(
                        [dict(page) for page in lecture_pages],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(
                        rows_with_image_revisions(lecture_figures),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps([], ensure_ascii=False, sort_keys=True),
                    PDF_EXPORT_VERSION,
                )
            except Exception as error:
                st.warning(
                    "PDF 다운로드 파일을 만들지 못했습니다: "
                    f"{error}"
                )
            else:
                st.subheader("AI 정리본 PDF 미리보기")
                # 미리보기는 기본으로 꺼둔다. `st.pdf`는 Streamlit 탭이
                # 화면에 안 보여도 매 재실행마다(= 앱 어디를 클릭해도)
                # 실행되는 코드 안에 있어서, 항상 그려두면 이 PDF(보통
                # 수 MB)를 매번 화면에 다시 내려보내게 된다.
                if st.toggle(
                    "PDF 미리보기 표시",
                    key=f'show_summary_pdf_preview_{selected_lecture["id"]}',
                ):
                    st.pdf(
                        summary_pdf,
                        height=800,
                        key=(
                            "summary_pdf_preview_"
                            f'{selected_lecture["id"]}_'
                            f"{PDF_EXPORT_VERSION}"
                        ),
                    )

                st.download_button(
                    "AI 정리본 PDF 다운로드",
                    data=summary_pdf,
                    file_name=(
                        f'{selected_subject["name"]}_'
                        f'{selected_lecture["lecture_no"]:02d}강_'
                        "정리본.pdf"
                    ),
                    mime="application/pdf",
                    use_container_width=True,
                    key=f'download_summary_pdf_{selected_lecture["id"]}',
                )

        st.divider()


    corrected_transcript_path = (
        selected_lecture["corrected_transcript_path"]
    )
    correction_is_available = bool(
        corrected_transcript_path
        and Path(corrected_transcript_path).exists()
    )
    transcript_path = (
        corrected_transcript_path
        if correction_is_available
        else selected_lecture["transcript_path"]
    )
    transcript_text = ""

    if transcript_path:
        saved_transcript = Path(
            transcript_path
        )

        if saved_transcript.exists():
            transcript_text = (
                saved_transcript.read_text(
                    encoding="utf-8-sig"
                )
            )

    if correction_is_available and transcript_text.strip():
        st.info(
            "이 정리본은 원본 대신 강의록 기반 AI 교정 전사문을 사용합니다."
        )


    if not lecture_pages:
        st.warning(
            "먼저 강의록 탭에서 PDF 텍스트를 "
            "추출해주세요."
        )

    if not transcript_text.strip():
        st.warning(
            "먼저 전사문 탭에서 TXT 전사문을 등록해주세요."
        )


    can_prepare = bool(
        lecture_pages
        and transcript_text.strip()
    )

    if can_prepare:
        codex_path = find_codex_cli()
        available_image_records = [
            page
            for page in lecture_pages
            if page["image_path"]
            and Path(page["image_path"]).exists()
        ]
        selected_image_pages = sorted({
            figure["page_no"]
            for figure in lecture_figures
        })

        if available_image_records:

            st.caption(
                f"아래 버튼을 누르면 전체 {len(available_image_records)}페이지 "
                "이미지가 여러 묶음으로 ChatGPT 구독의 Codex에 전송됩니다. "
                "AI가 글자뿐인 슬라이드는 제외하고 필요한 그림 위치를 찾으며, "
                "실제 크롭 파일은 이 Mac에 저장합니다."
            )

            if codex_path and st.button(
                "전체 강의록에서 핵심 그림 자동 찾기 + 크롭",
                use_container_width=True,
                key=(
                    f'detect_summary_figures_'
                    f'{selected_lecture["id"]}'
                ),
            ):
                figure_progress = st.progress(0.0)
                figure_status = st.empty()
                try:
                    logged_in, login_message = (
                        get_codex_login_status(codex_path)
                    )

                    if not logged_in:
                        raise RuntimeError(
                            "Codex 로그인이 필요합니다. "
                            f"{login_message}"
                        )

                    # 버튼을 눌렀을 때만 계산한다. 페이지마다 이미지를 열어
                    # 색 채도를 확인하는 작업(PIL)이라 페이지 수가 많으면
                    # 꽤 걸리는데, 예전에는 이 버튼을 누르지 않아도 탭이
                    # 열려 있는 한 매 재실행마다(= 앱 어디를 클릭해도)
                    # 계산해서 불필요하게 느렸다.
                    figure_candidate_records = select_figure_candidate_pages(
                        [dict(page) for page in available_image_records]
                    )

                    batch_size = 8
                    page_batches = [
                        figure_candidate_records[start:start + batch_size]
                        for start in range(
                            0,
                            len(figure_candidate_records),
                            batch_size,
                        )
                    ]
                    detections = []

                    for batch_no, page_batch in enumerate(
                        page_batches,
                        start=1,
                    ):
                        first_page = page_batch[0]["page_no"]
                        last_page = page_batch[-1]["page_no"]
                        figure_status.info(
                            f"AI 그림 탐색 {batch_no}/{len(page_batches)}묶음 · "
                            f"PDF {first_page}~{last_page}페이지 분석 중"
                        )
                        detections.extend(
                            detect_figure_regions_with_codex(
                                codex_path,
                                page_batch,
                            )
                        )
                        figure_progress.progress(
                            batch_no / len(page_batches)
                        )

                    pdf_path = selected_lecture["pdf_path"]
                    if pdf_path:
                        figure_dir = Path(pdf_path).parent / "figures"
                    else:
                        figure_dir = (
                            DATA_DIR
                            / selected_subject["name"]
                            / f'{selected_lecture["lecture_no"]:02d}'
                            / "figures"
                        )

                    cropped_figures = crop_detected_figures(
                        detections,
                        available_image_records,
                        figure_dir,
                    )
                    selected_image_pages = sorted({
                        figure["page_no"]
                        for figure in cropped_figures
                    })
                    replace_lecture_figures(
                        selected_lecture["id"],
                        cropped_figures,
                    )
                    update_summary_image_pages(
                        selected_lecture["id"],
                        selected_image_pages,
                    )
                except Exception as error:
                    figure_status.empty()
                    st.error(
                        "그림을 찾는 중 오류가 발생했습니다: "
                        f"{error}"
                    )
                else:
                    if cropped_figures:
                        figure_status.success(
                            f"완료 · 핵심 그림 {len(cropped_figures)}개를 "
                            "자동으로 찾아 크롭했습니다."
                        )
                    else:
                        figure_status.warning(
                            "전체 강의록에서 따로 넣을 만한 그림을 "
                            "찾지 못했습니다."
                        )
                    st.rerun()
        else:
            selected_image_pages = []
            st.warning(
                "강의록 탭에서 먼저 '페이지 이미지 생성 + "
                "한글 OCR'을 완료해야 이미지를 넣을 수 있습니다."
            )

        if lecture_figures:
            with st.expander(
                f"정리본에 사용할 크롭 그림 {len(lecture_figures)}개 보기",
                expanded=False,
            ):
                figure_columns = st.columns(2)
                for index, figure in enumerate(lecture_figures):
                    with figure_columns[index % 2]:
                        st.image(
                            figure["image_path"],
                            caption=(
                                f'PDF {figure["page_no"]}페이지 - '
                                f'{figure["caption"]}'
                            ),
                            use_container_width=True,
                        )
        elif available_image_records:
            st.info(
                "아직 크롭된 그림이 없습니다. "
                "'전체 강의록에서 핵심 그림 자동 찾기 + 크롭'을 실행해주세요."
            )

        st.markdown(
            "**자동 방식 - ChatGPT 구독 사용**"
        )

        if codex_path:
            st.caption(
                "이 Mac의 Codex 로그인을 사용합니다. "
                "별도 API 키는 필요하지 않습니다."
            )

            if st.button(
                "백그라운드에서 AI 정리 시작",
                type="primary",
                use_container_width=True,
                key=(
                    f'generate_codex_summary_'
                    f'{selected_lecture["id"]}'
                ),
            ):
                try:
                    lecture_pdf_path = selected_lecture["pdf_path"]
                    lecture_transcript_path = selected_lecture["transcript_path"]
                    if not lecture_pdf_path or not Path(lecture_pdf_path).exists():
                        raise FileNotFoundError("강의록 PDF 파일을 찾을 수 없습니다.")
                    if (
                        not lecture_transcript_path
                        or not Path(lecture_transcript_path).exists()
                    ):
                        raise FileNotFoundError("원본 전사문 TXT 파일을 찾을 수 없습니다.")

                    generation_state_path = (
                        Path(lecture_pdf_path).parent
                        / "generation_state.txt"
                    )
                    generation_state_path.write_text(
                        "pending",
                        encoding="utf-8",
                    )
                    _, job_created = enqueue_generation_job(
                        selected_lecture["id"]
                    )
                    ensure_generation_worker()
                except Exception as error:
                    st.error(
                        "백그라운드 생성 요청 중 오류가 "
                        f"발생했습니다: {error}"
                    )
                else:
                    st.success(
                        "백그라운드 생성 대기열에 "
                        + ("등록했습니다." if job_created else "이미 등록되어 있습니다.")
                    )
                    st.rerun()
        else:
            st.warning(
                "Codex CLI를 찾지 못했습니다. "
                "VS Code의 ChatGPT/Codex 로그인을 확인해주세요."
            )


# =========================================================
# 기출 분석 탭
# =========================================================

with tab4:
    st.subheader("기출 분석")

    if not saved_summary:
        st.info("AI 정리본을 먼저 생성해주세요.")
    elif not exam_summary_content:
        st.info("이 정리본에는 분석된 기출 문항이 없습니다.")
    else:
        exam_pdf_title = (
            f'# {selected_subject["name"]} - '
            f'{selected_lecture["lecture_no"]}강 기출 분석\n\n'
        )
        try:
            exam_pdf = build_cached_exam_pdf(
                exam_pdf_title + exam_summary_content,
                json.dumps(
                    rows_with_image_revisions(lecture_exam_figures),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                PDF_EXPORT_VERSION,
            )
        except Exception as error:
            st.warning(
                "기출 분석 PDF를 만들지 못했습니다: "
                f"{error}"
            )
        else:
            # 미리보기는 기본으로 꺼둔다. `st.pdf`는 Streamlit 탭이
            # 화면에 안 보여도 매 재실행마다(= 앱 어디를 클릭해도)
            # 실행되는 코드 안에 있어서, 항상 그려두면 이 PDF(보통
            # 수 MB)를 매번 화면에 다시 내려보내게 된다.
            if st.toggle(
                "PDF 미리보기 표시",
                key=f'show_exam_pdf_preview_{selected_lecture["id"]}',
            ):
                st.pdf(
                    exam_pdf,
                    height=800,
                    key=(
                        "exam_pdf_preview_"
                        f'{selected_lecture["id"]}_'
                        f"{PDF_EXPORT_VERSION}"
                    ),
                )

            st.download_button(
                "기출 분석 PDF 다운로드",
                data=exam_pdf,
                file_name=(
                    f'{selected_subject["name"]}_'
                    f'{selected_lecture["lecture_no"]:02d}강_'
                    "기출분석.pdf"
                ),
                mime="application/pdf",
                use_container_width=True,
                key=f'download_exam_pdf_{selected_lecture["id"]}',
            )


# =========================================================
# 복습 퀴즈 탭
# =========================================================

with tab5:
    st.subheader("복습 퀴즈")

    quiz_summary = get_lecture_summary(
        selected_lecture["id"]
    )

    if not quiz_summary:
        st.info(
            "AI 정리본을 생성하면 시험 직전 체크리스트가 "
            "복습 퀴즈로 자동 등록됩니다."
        )
    else:
        base_web_quiz = get_base_web_quiz(
            selected_lecture["id"]
        )
        base_quiz_content = (
            base_web_quiz["content"]
            if base_web_quiz
            else quiz_summary["content"]
        )
        quiz_items = extract_review_quiz(base_quiz_content)

        if not quiz_items:
            st.warning(
                "정리본에서 '6. 시험 직전 체크리스트'의 "
                "문제와 정답을 찾지 못했습니다."
            )
        else:
            has_legacy_quiz = any(
                len(item.get("choices", {})) != 4
                or item.get("correct_choice")
                not in item.get("choices", {})
                for item in quiz_items
            )

            if has_legacy_quiz:
                st.info(
                    "이 정리본은 이전 단답형 형식입니다. "
                    "아래 버튼으로 퀴즈 부분만 4지선다형으로 변환할 수 있습니다."
                )

                if st.button(
                    "기존 문항을 객관식으로 변환",
                    type="primary",
                    width="stretch",
                    key=f'convert_quiz_{selected_lecture["id"]}',
                ):
                    conversion_status = st.empty()
                    conversion_status.info(
                        "객관식 선택지와 해설을 생성하고 있습니다."
                    )

                    try:
                        conversion_codex_path = find_codex_cli()
                        if not conversion_codex_path:
                            raise RuntimeError("Codex CLI를 찾지 못했습니다.")

                        logged_in, login_message = get_codex_login_status(
                            conversion_codex_path
                        )
                        if not logged_in:
                            raise RuntimeError(
                                "Codex 로그인이 필요합니다. "
                                f"{login_message}"
                            )

                        source_questions = "\n\n".join(
                            (
                                f'{item["number"]}. {item["question"]}\n'
                                f'기존 정답: {item["answer_text"]}'
                            )
                            for item in quiz_items
                        )
                        conversion_request = f"""
아래 의학 강의 복습 문항을 내용과 정답의 의미를 바꾸지 않고
4지선다형 객관식 문제로 변환하세요.

각 문항은 반드시 다음 형식을 정확히 지키세요.

## 6. 시험 직전 체크리스트

1. 질문
A. 선택지
B. 선택지
C. 선택지
D. 선택지
**정답:** B
**해설:** 정답인 이유와 가장 헷갈리는 오답의 구별 기준

요구사항:
- 문항 수와 핵심 정답은 그대로 유지
- 정답은 A-D에 고르게 분산
- 오답은 강의 내용을 학습할 때 실제로 혼동하기 쉬운 개념으로 작성
- 정답 표시는 A, B, C, D 중 하나만 작성
- 위 6번 섹션만 Markdown으로 출력

원본 문항:
{source_questions}
""".strip()

                        converted_section = generate_summary_with_codex(
                            conversion_codex_path,
                            conversion_request,
                            image_paths=[],
                            timeout_seconds=600,
                        )
                        converted_items = extract_review_quiz(
                            converted_section
                        )
                        if (
                            len(converted_items) != len(quiz_items)
                            or any(
                                len(item.get("choices", {})) != 4
                                or item.get("correct_choice")
                                not in item.get("choices", {})
                                for item in converted_items
                            )
                        ):
                            raise RuntimeError(
                                "객관식 형식 검증에 실패했습니다. 다시 시도해주세요."
                            )

                        save_base_web_quiz(
                            selected_lecture["id"],
                            converted_section,
                            "Codex - ChatGPT 구독",
                        )
                    except Exception as error:
                        conversion_status.empty()
                        st.error(
                            "객관식 변환 중 오류가 발생했습니다: "
                            f"{error}"
                        )
                    else:
                        conversion_status.success(
                            "객관식 변환이 완료되었습니다."
                        )
                        st.rerun()

            additional_quiz_rows = get_additional_quiz_sets(
                selected_lecture["id"]
            )
            quiz_pages = [
                {
                    "label": "기본 문제",
                    "items": quiz_items,
                }
            ]

            for quiz_set in additional_quiz_rows:
                additional_items = extract_review_quiz(
                    quiz_set["content"]
                )
                if additional_items:
                    quiz_pages.append(
                        {
                            "label": f'추가 문제 {quiz_set["set_no"]}',
                            "items": additional_items,
                        }
                    )

            if st.button(
                "새로운 문제 10개 추가 생성",
                type="primary",
                width="stretch",
                key=f'generate_more_quiz_{selected_lecture["id"]}',
            ):
                generation_status = st.empty()
                generation_status.info(
                    "기존 문제와 겹치지 않는 새 문제를 생성하고 있습니다."
                )

                try:
                    quiz_codex_path = find_codex_cli()
                    if not quiz_codex_path:
                        raise RuntimeError("Codex CLI를 찾지 못했습니다.")

                    logged_in, login_message = get_codex_login_status(
                        quiz_codex_path
                    )
                    if not logged_in:
                        raise RuntimeError(
                            "Codex 로그인이 필요합니다. "
                            f"{login_message}"
                        )

                    existing_questions = "\n".join(
                        f'- {item["question"]}'
                        for page in quiz_pages
                        for item in page["items"]
                    )
                    exam_rows = get_lecture_exam_files(
                        selected_lecture["id"]
                    )
                    exam_source = "\n\n".join(
                        f'### {row["file_name"]}\n{row["extracted_text"]}'
                        for row in exam_rows
                        if row["extracted_text"]
                    )[:120000] or "(등록된 기출 없음)"
                    # 정리본은 항상 한국어로 번역되므로, 실제 시험 언어는
                    # 강의록에서 크롭한 기출 문항 원문과 과목 기출 PDF
                    # 원문에서만 확인할 수 있다.
                    exam_texts = [
                        figure["extracted_text"]
                        for figure in lecture_exam_figures
                        if figure["extracted_text"]
                    ] + [exam_source]
                    extra_quiz_content = generate_additional_multiple_choice_quiz(
                        quiz_codex_path,
                        quiz_summary["content"],
                        existing_questions,
                        exam_source,
                        exam_texts=exam_texts,
                    )
                    extra_quiz_items = extract_review_quiz(
                        extra_quiz_content
                    )
                    if not validate_multiple_choice_quiz(
                        extra_quiz_content
                    ):
                        raise RuntimeError(
                            "새 문제의 객관식 형식 검증에 실패했습니다. "
                            "다시 시도해주세요."
                        )

                    add_additional_quiz_set(
                        selected_lecture["id"],
                        extra_quiz_content,
                        "Codex - ChatGPT 구독",
                    )
                    # 클라우드 학습 앱이 설정된 경우 새 문제도 즉시 복제한다.
                    try:
                        try_sync_lecture_to_cloud(selected_lecture["id"])
                    except Exception as cloud_error:
                        st.warning(
                            "문제는 저장했지만 클라우드 동기화에 실패했습니다: "
                            f"{cloud_error}"
                        )
                except Exception as error:
                    generation_status.empty()
                    st.error(
                        "새 문제 생성 중 오류가 발생했습니다: "
                        f"{error}"
                    )
                else:
                    generation_status.success(
                        "새 문제 10개를 추가했습니다. 아래 목록에서 바로 볼 수 있습니다."
                    )
                    st.rerun()

            # 세트별로 10개씩 나눠 생성하지만, 열람은 모든 세트를 하나로
            # 합쳐 한 번에 넘나들 수 있게 한다("추가 문제 N" 페이지를
            # 따로 넘겨봐야 하는 불편함을 없애기 위함).
            quiz_items = []
            for page in quiz_pages:
                for item in page["items"]:
                    item["_source_label"] = page["label"]
                    quiz_items.append(item)

            try:
                refresh_cloud_quiz_progress(selected_lecture["id"])
            except Exception as cloud_error:
                st.caption(f"클라우드 복습 기록 동기화 지연: {cloud_error}")

            quiz_progress = get_quiz_progress(
                selected_lecture["id"]
            )

            total_count = len(quiz_items)
            mastered_count = sum(
                1
                for item in quiz_items
                if quiz_progress.get(item["question_key"])
                and quiz_progress[item["question_key"]]["review_status"]
                == "mastered"
            )
            review_count = sum(
                1
                for item in quiz_items
                if quiz_progress.get(item["question_key"])
                and quiz_progress[item["question_key"]]["review_status"]
                == "review"
            )
            unseen_count = total_count - mastered_count - review_count
            due_count = sum(
                1
                for item in quiz_items
                if is_due(
                    quiz_progress[item["question_key"]]["due_date"]
                    if item["question_key"] in quiz_progress
                    else None
                )
            )

            metric1, metric2, metric3, metric4, metric5 = st.columns(5)
            metric1.metric("전체", total_count)
            metric2.metric("오늘 복습", due_count)
            metric3.metric("미학습", unseen_count)
            metric4.metric("복습 필요", review_count)
            metric5.metric("학습 완료", mastered_count)

            if total_count:
                st.progress(mastered_count / total_count)

            filter_label = st.radio(
                "문제 보기",
                ["전체", "오늘 복습", "미학습", "오답 기록", "학습 완료"],
                horizontal=True,
                key=f'quiz_filter_{selected_lecture["id"]}',
            )

            status_labels = {
                "unseen": "미학습",
                "review": "복습 필요",
                "mastered": "학습 완료",
            }

            # 목록은 필터를 새로 고를 때만 계산해 그 상태로 고정해 둔다.
            # 매번 다시 계산하면, "미학습" 필터로 보다가 문제를 풀어
            # 상태가 바뀌는 순간 방금 푼 문제가 목록에서 바로 사라져
            # 채점 결과도 못 보고 다음 문제로 밀려나는 문제가 있었다.
            index_state_key = f'quiz_item_index_{selected_lecture["id"]}'
            filter_track_key = (
                f'quiz_item_filter_track_{selected_lecture["id"]}'
            )
            filter_snapshot_key = (
                f'quiz_item_filter_snapshot_{selected_lecture["id"]}'
            )
            if st.session_state.get(filter_track_key) != filter_label:
                snapshot_keys = []
                for item in quiz_items:
                    progress = quiz_progress.get(item["question_key"])
                    status = progress["review_status"] if progress else "unseen"
                    incorrect_count = (
                        progress["incorrect_count"] if progress else 0
                    )
                    due_date = progress["due_date"] if progress else None

                    if filter_label == "오늘 복습" and not is_due(due_date):
                        continue
                    if filter_label == "미학습" and status != "unseen":
                        continue
                    if filter_label == "오답 기록" and incorrect_count < 1:
                        continue
                    if filter_label == "학습 완료" and status != "mastered":
                        continue

                    snapshot_keys.append(item["question_key"])

                if filter_label == "오늘 복습":
                    # 가장 기한이 지난(오래 미룬) 문제부터 보여준다.
                    snapshot_keys.sort(
                        key=lambda key: quiz_progress[key]["due_date"]
                    )

                st.session_state[filter_snapshot_key] = snapshot_keys
                st.session_state[index_state_key] = 0
                st.session_state[filter_track_key] = filter_label

            items_by_key = {
                item["question_key"]: item for item in quiz_items
            }
            filtered_items = [
                items_by_key[key]
                for key in st.session_state.get(filter_snapshot_key, [])
                if key in items_by_key
            ]

            if not filtered_items:
                st.info("이 조건에 해당하는 문제가 없습니다.")
            else:
                current_index = min(
                    st.session_state.get(index_state_key, 0),
                    len(filtered_items) - 1,
                )
                st.session_state[index_state_key] = current_index
                item = filtered_items[current_index]

                progress = quiz_progress.get(item["question_key"])
                status = progress["review_status"] if progress else "unseen"
                incorrect_count = progress["incorrect_count"] if progress else 0
                correct_count = progress["correct_count"] if progress else 0
                due_date = progress["due_date"] if progress else None

                with st.container(border=True):
                    nav_prev, nav_label, nav_next = st.columns([1, 2, 1])
                    with nav_prev:
                        st.button(
                            "◀ 이전 문제",
                            disabled=(current_index == 0),
                            width="stretch",
                            key=f'quiz_item_prev_{selected_lecture["id"]}',
                            on_click=change_quiz_item_index,
                            args=(index_state_key, -1, len(filtered_items)),
                        )
                    with nav_label:
                        st.markdown(
                            "<p style='text-align:center; margin-top:0.45rem;'>"
                            f"<b>문제 {current_index + 1} / {len(filtered_items)}</b>"
                            "</p>",
                            unsafe_allow_html=True,
                        )
                    with nav_next:
                        st.button(
                            "다음 문제 ▶",
                            disabled=(current_index == len(filtered_items) - 1),
                            width="stretch",
                            key=f'quiz_item_next_{selected_lecture["id"]}',
                            on_click=change_quiz_item_index,
                            args=(index_state_key, 1, len(filtered_items)),
                        )

                    question_col, status_col = st.columns([5, 1])

                    with question_col:
                        label_prefix = (
                            f'[{item["_source_label"]}] '
                            if len(quiz_pages) > 1
                            else ""
                        )
                        st.markdown(
                            f'**{label_prefix}Q{item["number"]}. {item["question"]}**'
                        )

                    with status_col:
                        st.caption(status_labels[status])

                    has_multiple_choices = (
                        len(item.get("choices", {})) == 4
                        and item.get("correct_choice")
                        in item.get("choices", {})
                    )

                    if has_multiple_choices:
                        result_key = (
                            f'quiz_result_{selected_lecture["id"]}_'
                            f'{item["question_key"]}'
                        )
                        result = st.session_state.get(result_key)

                        if result is None:
                            # 아직 안 푼 문제: 선택지를 각각 버튼으로 두고
                            # 클릭 한 번으로 바로 채점한다. 채점은
                            # on_click 콜백(`submit_quiz_answer`)에서
                            # 처리하는데, 콜백은 화면이 다시 그려지기
                            # 전에 실행되므로 `st.rerun()`을 따로 부르지
                            # 않아도 클릭한 바로 그 재실행에서 결과가
                            # 보인다.
                            for choice_key in ("A", "B", "C", "D"):
                                st.button(
                                    f'{choice_key}. {item["choices"][choice_key]}',
                                    width="stretch",
                                    key=(
                                        f'quiz_choice_{selected_lecture["id"]}_'
                                        f'{item["question_key"]}_{choice_key}'
                                    ),
                                    on_click=submit_quiz_answer,
                                    args=(
                                        selected_lecture["id"],
                                        item,
                                        choice_key,
                                        result_key,
                                    ),
                                )
                        else:
                            # 이미 푼 문제: 버튼 대신 정답·오답을 색으로
                            # 뚜렷하게 구분해 보여준다(정답은 초록,
                            # 사용자가 고른 오답은 빨강, 나머지는 그대로).
                            selected_choice = result["selected_choice"]
                            for choice_key in ("A", "B", "C", "D"):
                                choice_text = (
                                    f'{choice_key}. '
                                    f'{item["choices"][choice_key]}'
                                )
                                if choice_key == item["correct_choice"]:
                                    st.success(f"✅ {choice_text}")
                                elif choice_key == selected_choice:
                                    st.error(f"❌ {choice_text}")
                                else:
                                    st.write(choice_text)

                            if result["is_correct"]:
                                st.success("정답입니다.")
                            else:
                                st.error(
                                    "오답입니다. 복습 필요 문제로 저장했습니다."
                                )

                            if item.get("explanation"):
                                st.caption(
                                    f'해설 · {item["explanation"]}'
                                )

                            if st.button(
                                "다시 풀기",
                                key=(
                                    f'quiz_retry_{selected_lecture["id"]}_'
                                    f'{item["question_key"]}'
                                ),
                            ):
                                del st.session_state[result_key]
                                st.rerun()
                    else:
                        st.caption(
                            "이 문항은 이전 단답형 정리본에서 가져왔습니다. "
                            "새로 생성한 정리본부터 객관식 자동 채점이 적용됩니다."
                        )
                        with st.expander("정답 확인", expanded=False):
                            st.success(item["answer"])

                    due_caption = (
                        f" · 다음 복습 예정일 {due_date}" if due_date else ""
                    )
                    st.caption(
                        f"맞힌 횟수 {correct_count}회 · "
                        f"틀린 횟수 {incorrect_count}회{due_caption}"
                    )

            with st.expander("이 강의의 복습 기록 초기화", expanded=False):
                confirm_quiz_reset = st.checkbox(
                    "맞힌 횟수와 오답 기록을 모두 초기화합니다.",
                    key=f'confirm_quiz_reset_{selected_lecture["id"]}',
                )

                if st.button(
                    "복습 기록 초기화",
                    disabled=not confirm_quiz_reset,
                    key=f'reset_quiz_progress_{selected_lecture["id"]}',
                ):
                    reset_quiz_progress(selected_lecture["id"])
                    st.rerun()
