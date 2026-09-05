import hashlib
import hmac
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from quiz_utils import extract_review_quiz
from cloud.cloud_backend import CloudStudyRepository


st.set_page_config(page_title="MedStudy 학습", page_icon="📚", layout="wide")


def require_password():
    expected = str(st.secrets["app"]["password"])
    if st.session_state.get("authenticated"):
        return
    st.title("MedStudy")
    password = st.text_input("비밀번호", type="password")
    if st.button("로그인", type="primary", use_container_width=True):
        if hmac.compare_digest(
            hashlib.sha256(password.encode()).digest(),
            hashlib.sha256(expected.encode()).digest(),
        ):
            st.session_state.authenticated = True
            st.rerun()
        st.error("비밀번호가 맞지 않습니다.")
    st.stop()


@st.cache_resource
def repository():
    settings = st.secrets["supabase"]
    return CloudStudyRepository(
        create_client(settings["url"], settings["service_key"]),
        settings.get("bucket", "medstudy"),
    )


def change_page(key, delta):
    st.session_state[key] += delta


require_password()
repo = repository()
st.title("MedStudy")
st.caption("정리본 열람과 객관식 복습 전용")

try:
    subjects = repo.subjects()
except Exception as error:
    st.error(f"클라우드 데이터에 연결하지 못했습니다: {error}")
    st.stop()

if not subjects:
    st.info("아직 동기화된 과목이 없습니다.")
    st.stop()

subject_labels = {row["id"]: row["name"] for row in subjects}
subject_id = st.sidebar.radio(
    "과목",
    list(subject_labels),
    format_func=subject_labels.get,
)
lectures = repo.lectures(subject_id)
if not lectures:
    st.info("이 과목에는 동기화된 강의가 없습니다.")
    st.stop()

lecture_labels = {
    row["id"]: f'{row["lecture_no"]}강 {row["title"]}'.strip()
    for row in lectures
}
lecture_id = st.sidebar.radio(
    "강의",
    list(lecture_labels),
    format_func=lecture_labels.get,
)
lecture = next(row for row in lectures if row["id"] == lecture_id)
st.header(lecture_labels[lecture_id])

pdf_tab, quiz_tab = st.tabs(["정리본", "복습 퀴즈"])

with pdf_tab:
    if not lecture.get("summary_pdf_path"):
        st.info("동기화된 정리본 PDF가 없습니다.")
    else:
        pdf_url = repo.signed_pdf_url(lecture["summary_pdf_path"])
        if pdf_url:
            components.iframe(pdf_url, height=900, scrolling=True)
            st.link_button("정리본 PDF 열기", pdf_url, use_container_width=True)
        else:
            st.error("정리본 PDF 주소를 만들지 못했습니다.")

with quiz_tab:
    rows = repo.quiz_sets(lecture_id)
    pages = []
    for row in rows:
        items = extract_review_quiz(row["content"])
        if items:
            pages.append({
                "label": "기본 문제" if row["set_no"] == 0 else f'추가 문제 {row["set_no"]}',
                "items": items,
            })
    if not pages:
        st.info("동기화된 객관식 문제가 없습니다.")
        st.stop()

    page_key = f"quiz_page_{lecture_id}"
    st.session_state.setdefault(page_key, 0)
    st.session_state[page_key] = min(st.session_state[page_key], len(pages) - 1)
    left, center, right = st.columns([1, 2, 1])
    left.button(
        "이전 페이지", disabled=st.session_state[page_key] == 0,
        on_click=change_page, args=(page_key, -1), use_container_width=True,
    )
    center.markdown(
        f'<p style="text-align:center"><b>{pages[st.session_state[page_key]]["label"]}</b> · '
        f'{st.session_state[page_key] + 1}/{len(pages)}</p>',
        unsafe_allow_html=True,
    )
    right.button(
        "다음 페이지", disabled=st.session_state[page_key] == len(pages) - 1,
        on_click=change_page, args=(page_key, 1), use_container_width=True,
    )

    items = pages[st.session_state[page_key]]["items"]
    progress = repo.progress(lecture_id)
    filter_label = st.radio(
        "문제 보기", ["전체", "복습 필요", "오답 기록", "학습 완료"],
        horizontal=True, key=f"filter_{lecture_id}",
    )
    visible = []
    for item in items:
        saved = progress.get(item["question_key"], {})
        status = saved.get("review_status", "unseen")
        wrong = int(saved.get("incorrect_count", 0))
        if filter_label == "복습 필요" and status != "review":
            continue
        if filter_label == "오답 기록" and wrong == 0:
            continue
        if filter_label == "학습 완료" and status != "mastered":
            continue
        visible.append(item)

    if not visible:
        st.info("이 조건에 해당하는 문제가 없습니다.")
    for item in visible:
        saved = progress.get(item["question_key"], {})
        with st.container(border=True):
            st.markdown(f'**Q{item["number"]}. {item["question"]}**')
            choices = item.get("choices", {})
            if len(choices) != 4 or item.get("correct_choice") not in choices:
                st.caption("이 문항은 자동 채점할 수 없는 이전 형식입니다.")
                continue
            options = [f"{key}. {choices[key]}" for key in "ABCD"]
            choice_key = f'choice_{lecture_id}_{item["question_key"]}'
            selected = st.radio(
                "정답 선택", options, index=None, key=choice_key,
                label_visibility="collapsed",
            )
            if st.button(
                "정답 제출", key=f'submit_{lecture_id}_{item["question_key"]}',
                disabled=selected is None, type="primary", use_container_width=True,
            ):
                is_correct = selected[0] == item["correct_choice"]
                repo.save_result(lecture_id, item, is_correct)
                st.session_state[f'result_{lecture_id}_{item["question_key"]}'] = is_correct
                st.rerun()
            result = st.session_state.get(f'result_{lecture_id}_{item["question_key"]}')
            if result is True:
                st.success("정답입니다.")
            elif result is False:
                st.error("오답입니다. 복습 필요 문제로 저장했습니다.")
            if result is not None:
                st.info(f'정답: {item["correct_choice"]}. {item["answer_text"]}')
                if item.get("explanation"):
                    st.caption(f'해설 · {item["explanation"]}')
            st.caption(
                f'맞힌 횟수 {saved.get("correct_count", 0)}회 · '
                f'틀린 횟수 {saved.get("incorrect_count", 0)}회'
            )

    with st.expander("이 강의의 복습 기록 초기화", expanded=False):
        if st.button("복습 기록 초기화", key=f"reset_{lecture_id}"):
            repo.reset_progress(lecture_id)
            st.rerun()
