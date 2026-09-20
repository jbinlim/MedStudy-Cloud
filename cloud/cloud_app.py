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
from spaced_repetition import is_due
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


def submit_global_quiz_answer(repo, lecture_id, item, choice_key, result_key):
    """'오늘의 복습' 화면(여러 강의 통합)에서 쓰는 채점 콜백.

    이 화면은 여러 강의를 넘나들므로, 문제별 강의의 진행률을 미리
    한 번에 읽어 두지 않는다. `save_result`가 필요하면 알아서 해당
    강의 진행률을 다시 읽어오므로 문제없다.
    """
    is_correct = choice_key == item["correct_choice"]
    repo.save_result(lecture_id, item, is_correct)
    st.session_state[result_key] = {
        "selected_choice": choice_key,
        "is_correct": is_correct,
    }


@st.cache_data(ttl=30, show_spinner=False)
def get_cached_global_review_queue(_repo):
    return _repo.global_review_queue()


def load_lecture_quiz_items_by_key(repo, lecture_id):
    """'오늘의 복습' 화면에서 강의별 문제 본문·선택지를 다시 찾아본다.

    이 화면은 여러 강의를 넘나들며 quiz_progress 행만 먼저 조회하는데,
    실제 문제 본문·선택지는 각 강의의 퀴즈 세트 안에만 있어 강의별로
    다시 구성해야 한다.
    """
    items = []
    for row in get_cached_quiz_sets(repo, lecture_id):
        items.extend(extract_review_quiz(row["content"]))
    return {item["question_key"]: item for item in items}


require_password()
repo = repository()
st.title("MedStudy")
st.caption("정리본 열람과 객관식 복습 전용")

if "cloud_app_view" not in st.session_state:
    st.session_state["cloud_app_view"] = "lecture"

if st.session_state["cloud_app_view"] == "review":
    if st.sidebar.button("← 강의 화면으로", use_container_width=True):
        st.session_state["cloud_app_view"] = "lecture"
        st.rerun()
else:
    if st.sidebar.button(
        "🔁 오늘의 복습 · 오답 노트", use_container_width=True
    ):
        st.session_state["cloud_app_view"] = "review"
        st.rerun()

# =========================================================
# 오늘의 복습 · 오답 노트 (모든 과목·강의 통합)
# =========================================================

if st.session_state["cloud_app_view"] == "review":
    st.header("오늘의 복습 · 오답 노트")
    st.caption(
        "간격 반복(SM-2) 일정상 오늘 복습할 문제와, 과목을 넘나들며 "
        "한 번이라도 틀린 문제를 한 곳에 모아 보여줍니다."
    )

    try:
        progress_rows = get_cached_global_review_queue(repo)
    except Exception as error:
        st.error(f"클라우드 데이터에 연결하지 못했습니다: {error}")
        st.stop()

    if not progress_rows:
        st.info("오늘 복습할 문제나 오답 기록이 없습니다.")
        st.stop()

    # 문제 본문·선택지는 각 강의의 퀴즈 세트에만 있으므로, 이 화면에
    # 등장하는 강의만 한 번씩 불러와 question_key로 다시 찾아 붙인다.
    lecture_ids = sorted({row["lecture_id"] for row in progress_rows})
    items_by_lecture = {
        lid: load_lecture_quiz_items_by_key(repo, lid) for lid in lecture_ids
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
        key="cloud_global_review_filter",
    )

    index_state_key = "cloud_global_review_index"
    filter_track_key = "cloud_global_review_filter_track"
    filter_snapshot_key = "cloud_global_review_filter_snapshot"

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
                use_container_width=True,
                key="cloud_global_review_prev",
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
                key="cloud_global_review_next",
                on_click=change_quiz_item_index,
                args=(index_state_key, 1, len(filtered_items)),
            )

        badge_col, open_col = st.columns([4, 1])
        with badge_col:
            st.caption(f'{item["_subject_name"]} · {item["_lecture_label"]}')
        with open_col:
            if st.button(
                "강의로 이동",
                key=f'cloud_go_to_lecture_{item["_composite_key"]}',
                use_container_width=True,
            ):
                st.session_state["cloud_subject_radio"] = item["_subject_id"]
                st.session_state["cloud_lecture_radio"] = lecture_id
                st.session_state["cloud_app_view"] = "lecture"
                st.rerun()

        st.markdown(f'**Q{item["number"]}. {item["question"]}**')

        result_key = f'quiz_result_{lecture_id}_{item["question_key"]}'
        result = st.session_state.get(result_key)

        if result is None:
            for choice_key in ("A", "B", "C", "D"):
                st.button(
                    f'{choice_key}. {item["choices"][choice_key]}',
                    use_container_width=True,
                    key=f'cloud_global_choice_{item["_composite_key"]}_{choice_key}',
                    on_click=submit_global_quiz_answer,
                    args=(repo, lecture_id, item, choice_key, result_key),
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
                key=f'cloud_global_retry_{item["_composite_key"]}',
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
    key="cloud_subject_radio",
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
    key="cloud_lecture_radio",
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
    due_count = sum(
        1
        for item in quiz_items
        if is_due(progress.get(item["question_key"], {}).get("due_date"))
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
        "문제 보기", ["전체", "오늘 복습", "미학습", "오답 기록", "학습 완료"],
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
            due_date = saved["due_date"] if saved else None

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
            snapshot_keys.sort(key=lambda key: progress[key]["due_date"])

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
        due_date = saved["due_date"] if saved else None

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

            due_caption = f" · 다음 복습 예정일 {due_date}" if due_date else ""
            st.caption(
                f"맞힌 횟수 {correct_count}회 · 틀린 횟수 {incorrect_count}회"
                f"{due_caption}"
            )

    with st.expander("이 강의의 복습 기록 초기화", expanded=False):
        if st.button("복습 기록 초기화", key=f"reset_{lecture_id}"):
            repo.reset_progress(lecture_id)
            st.rerun()
