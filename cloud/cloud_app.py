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


# 과목/강의 목록과 문제 세트는 자주 바뀌지 않으므로 짧게 캐시해서, 문제를
# 채점할 때마다 앱 전체가 다시 그려지며 이 셋을 매번 새로 조회하지 않게
# 한다(캐시가 없으면 정답 버튼 하나 누를 때마다 이 목록들까지 전부 다시
# 네트워크로 받아와 채점이 느리게 느껴졌다).
@st.cache_data(ttl=30, show_spinner=False)
def get_cached_subjects(_repo):
    return _repo.subjects()


@st.cache_data(ttl=30, show_spinner=False)
def get_cached_lectures(_repo, subject_id):
    return _repo.lectures(subject_id)


@st.cache_data(ttl=30, show_spinner=False)
def get_cached_quiz_sets(_repo, lecture_id):
    return _repo.quiz_sets(lecture_id)


def change_quiz_item_index(index_state_key, delta, item_count):
    """문제를 한 번에 하나씩 앞뒤로 넘긴다."""
    current = st.session_state.get(index_state_key, 0)
    st.session_state[index_state_key] = max(
        0, min(item_count - 1, current + delta)
    )


def submit_cloud_quiz_answer(repo, lecture_id, item, choice_key, result_key, progress):
    """선택지 버튼의 on_click 콜백으로 채점까지 한 번에 끝낸다.

    콜백은 화면이 다시 그려지기 전에 실행되므로, 버튼을 누른 바로 그
    재실행에서 이미 채점된 결과를 볼 수 있다. `progress`는 이번 화면을
    그리며 이미 한 번 읽어 둔 진행률이라, 채점 저장 시 맞힌/틀린 횟수를
    계산하려고 또 한 번 전체 진행률을 네트워크로 다시 읽어오지 않는다.
    """
    is_correct = choice_key == item["correct_choice"]
    repo.save_result(lecture_id, item, is_correct, previous_progress=progress)
    st.session_state[result_key] = {
        "selected_choice": choice_key,
        "is_correct": is_correct,
    }


require_password()
repo = repository()
st.title("MedStudy")
st.caption("정리본 열람과 객관식 복습 전용")

try:
    subjects = get_cached_subjects(repo)
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
lectures = get_cached_lectures(repo, subject_id)
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

pdf_tab, exam_tab, quiz_tab = st.tabs(["정리본", "기출 분석", "복습 퀴즈"])

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

with exam_tab:
    if not lecture.get("exam_pdf_path"):
        st.info("이 강의에는 분석된 기출 문항이 없습니다.")
    else:
        exam_pdf_url = repo.signed_pdf_url(lecture["exam_pdf_path"])
        if exam_pdf_url:
            components.iframe(exam_pdf_url, height=900, scrolling=True)
            st.link_button("기출 분석 PDF 열기", exam_pdf_url, use_container_width=True)
        else:
            st.error("기출 분석 PDF 주소를 만들지 못했습니다.")

with quiz_tab:
    rows = get_cached_quiz_sets(repo, lecture_id)
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

    # 세트별로 10개씩 생성되지만, 열람은 모든 세트를 하나로 합쳐
    # 한 번에 넘나들 수 있게 한다.
    quiz_items = []
    for page in pages:
        for item in page["items"]:
            item["_source_label"] = page["label"]
            quiz_items.append(item)

    progress = repo.progress(lecture_id)

    total_count = len(quiz_items)
    mastered_count = sum(
        1
        for item in quiz_items
        if progress.get(item["question_key"], {}).get("review_status") == "mastered"
    )
    review_count = sum(
        1
        for item in quiz_items
        if progress.get(item["question_key"], {}).get("review_status") == "review"
    )
    unseen_count = total_count - mastered_count - review_count

    metric1, metric2, metric3, metric4 = st.columns(4)
    metric1.metric("전체", total_count)
    metric2.metric("미학습", unseen_count)
    metric3.metric("복습 필요", review_count)
    metric4.metric("학습 완료", mastered_count)
    if total_count:
        st.progress(mastered_count / total_count)

    filter_label = st.radio(
        "문제 보기", ["전체", "미학습", "오답 기록", "학습 완료"],
        horizontal=True, key=f"filter_{lecture_id}",
    )

    status_labels = {"unseen": "미학습", "review": "복습 필요", "mastered": "학습 완료"}

    # 목록은 필터를 새로 고를 때만 계산해 그 상태로 고정해 둔다. 매번
    # 다시 계산하면, 필터로 보다가 문제를 풀어 상태가 바뀌는 순간 방금
    # 푼 문제가 목록에서 바로 사라져 채점 결과도 못 보고 다음 문제로
    # 밀려나는 문제가 있었다.
    index_state_key = f"quiz_item_index_{lecture_id}"
    filter_track_key = f"quiz_item_filter_track_{lecture_id}"
    filter_snapshot_key = f"quiz_item_filter_snapshot_{lecture_id}"

    if st.session_state.get(filter_track_key) != filter_label:
        snapshot_keys = []
        for item in quiz_items:
            saved = progress.get(item["question_key"])
            status = saved["review_status"] if saved else "unseen"
            incorrect_count = saved["incorrect_count"] if saved else 0

            if filter_label == "미학습" and status != "unseen":
                continue
            if filter_label == "오답 기록" and incorrect_count < 1:
                continue
            if filter_label == "학습 완료" and status != "mastered":
                continue

            snapshot_keys.append(item["question_key"])

        st.session_state[filter_snapshot_key] = snapshot_keys
        st.session_state[index_state_key] = 0
        st.session_state[filter_track_key] = filter_label

    items_by_key = {item["question_key"]: item for item in quiz_items}
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

        saved = progress.get(item["question_key"])
        status = saved["review_status"] if saved else "unseen"
        incorrect_count = saved["incorrect_count"] if saved else 0
        correct_count = saved["correct_count"] if saved else 0

        with st.container(border=True):
            nav_prev, nav_label, nav_next = st.columns([1, 2, 1])
            with nav_prev:
                st.button(
                    "◀ 이전 문제",
                    disabled=(current_index == 0),
                    use_container_width=True,
                    key=f"quiz_item_prev_{lecture_id}",
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
                    use_container_width=True,
                    key=f"quiz_item_next_{lecture_id}",
                    on_click=change_quiz_item_index,
                    args=(index_state_key, 1, len(filtered_items)),
                )

            question_col, status_col = st.columns([5, 1])
            with question_col:
                label_prefix = f'[{item["_source_label"]}] ' if len(pages) > 1 else ""
                st.markdown(f'**{label_prefix}Q{item["number"]}. {item["question"]}**')
            with status_col:
                st.caption(status_labels[status])

            choices = item.get("choices", {})
            has_multiple_choices = (
                len(choices) == 4 and item.get("correct_choice") in choices
            )

            if has_multiple_choices:
                result_key = f'quiz_result_{lecture_id}_{item["question_key"]}'
                result = st.session_state.get(result_key)

                if result is None:
                    # 아직 안 푼 문제: 선택지를 각각 버튼으로 두고 클릭
                    # 한 번으로 바로 채점한다(라디오로 고른 뒤 별도
                    # "정답 제출" 버튼을 또 누르는 방식보다 한 단계
                    # 적고, on_click 콜백이 재실행 전에 채점을 끝내
                    # st.rerun()을 따로 부르지 않아도 된다).
                    for choice_key in ("A", "B", "C", "D"):
                        st.button(
                            f'{choice_key}. {choices[choice_key]}',
                            use_container_width=True,
                            key=f'quiz_choice_{lecture_id}_{item["question_key"]}_{choice_key}',
                            on_click=submit_cloud_quiz_answer,
                            args=(repo, lecture_id, item, choice_key, result_key, progress),
                        )
                else:
                    selected_choice = result["selected_choice"]
                    for choice_key in ("A", "B", "C", "D"):
                        choice_text = f'{choice_key}. {choices[choice_key]}'
                        if choice_key == item["correct_choice"]:
                            st.success(f"✅ {choice_text}")
                        elif choice_key == selected_choice:
                            st.error(f"❌ {choice_text}")
                        else:
                            st.write(choice_text)

                    if result["is_correct"]:
                        st.success("정답입니다.")
                    else:
                        st.error("오답입니다. 복습 필요 문제로 저장했습니다.")

                    if item.get("explanation"):
                        st.caption(f'해설 · {item["explanation"]}')

                    if st.button(
                        "다시 풀기",
                        key=f'quiz_retry_{lecture_id}_{item["question_key"]}',
                    ):
                        del st.session_state[result_key]
                        st.rerun()
            else:
                st.caption(
                    "이 문항은 자동 채점할 수 없는 이전 형식입니다."
                )
                with st.expander("정답 확인", expanded=False):
                    st.success(item.get("answer", ""))

            st.caption(
                f"맞힌 횟수 {correct_count}회 · 틀린 횟수 {incorrect_count}회"
            )

    with st.expander("이 강의의 복습 기록 초기화", expanded=False):
        if st.button("복습 기록 초기화", key=f"reset_{lecture_id}"):
            repo.reset_progress(lecture_id)
            st.rerun()
