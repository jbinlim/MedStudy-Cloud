"""SM-2 알고리즘 기반 간격 반복(spaced repetition) 스케줄링.

객관식 퀴즈는 정답/오답 이진 결과만 나오므로, SM-2가 원래 쓰는 0~5점
품질 척도 대신 정답은 quality 5, 오답은 quality 2로 매핑해 표준 공식을
그대로 적용한다. 로컬 Streamlit 앱(database.py)과 iPad용 클라우드 앱
(cloud/cloud_backend.py)이 동일한 결과를 계산하도록 이 모듈 하나만
공유해서 쓴다.
"""

from datetime import date, timedelta

DEFAULT_EASE_FACTOR = 2.5
MIN_EASE_FACTOR = 1.3

# 이 값(일) 이상으로 복습 간격이 늘어나면 "학습 완료"로 표시한다.
# 정답을 2번 연속 맞혀야 도달하는 값이라, 한 번 맞혔다고 바로
# 완료 처리되던 이전 방식보다 실제 습득 여부를 더 잘 반영한다.
MASTERED_INTERVAL_DAYS = 6


def compute_next_review(previous, is_correct, today=None):
    """정답 여부를 반영해 다음 복습 일정을 계산한다.

    `previous`는 이전 quiz_progress 행(dict, sqlite3.Row, 또는 아직
    한 번도 안 풀었다면 None/빈 값). 반환값은 quiz_progress에 그대로
    저장할 수 있는 필드 딕셔너리다.
    """
    today = today or date.today()
    previous = previous or {}

    repetitions = int(previous.get("repetitions") or 0)
    ease_factor = float(previous.get("ease_factor") or DEFAULT_EASE_FACTOR)
    interval_days = int(previous.get("interval_days") or 0)

    quality = 5 if is_correct else 2

    # SM-2 표준 순서: 다음 간격은 "이번 복습 전" 이지수(ease factor)로
    # 계산하고, 이지수 자체는 그 다음에 갱신한다.
    if quality < 3:
        repetitions = 0
        interval_days = 1
    else:
        repetitions += 1
        if repetitions == 1:
            interval_days = 1
        elif repetitions == 2:
            interval_days = 6
        else:
            interval_days = round(interval_days * ease_factor)

    ease_factor += 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
    ease_factor = max(MIN_EASE_FACTOR, ease_factor)

    due_date = today + timedelta(days=interval_days)
    review_status = (
        "mastered" if interval_days >= MASTERED_INTERVAL_DAYS else "review"
    )

    return {
        "repetitions": repetitions,
        "ease_factor": round(ease_factor, 2),
        "interval_days": interval_days,
        "due_date": due_date.isoformat(),
        "review_status": review_status,
    }


def is_due(due_date_str, today=None):
    """오늘(또는 지정일) 기준으로 복습 대상인지 확인한다.

    아직 한 번도 안 풀어 `due_date`가 없는 문제는 "오늘 복습" 큐에서는
    빼되(전체/미학습 필터에서는 이미 보이므로), 기한이 지난 문제는
    항상 대상으로 포함한다.
    """
    if not due_date_str:
        return False
    today = today or date.today()
    return date.fromisoformat(str(due_date_str)) <= today
