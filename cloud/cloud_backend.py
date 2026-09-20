from datetime import date, datetime, timezone

from spaced_repetition import compute_next_review


class CloudStudyRepository:
    def __init__(self, client, bucket="medstudy"):
        self.client = client
        self.bucket = bucket

    def subjects(self):
        return (
            self.client.table("medstudy_subjects")
            .select("id,name")
            .order("name")
            .execute()
            .data
            or []
        )

    def lectures(self, subject_id):
        return (
            self.client.table("medstudy_lectures")
            .select(
                "id,subject_id,lecture_no,title,summary_pdf_path,"
                "exam_pdf_path,summary_updated_at"
            )
            .eq("subject_id", subject_id)
            .order("lecture_no")
            .execute()
            .data
            or []
        )

    def all_lectures(self):
        """과목 구분 없이 모든 강의를 가져온다(전체 복습 큐 화면용)."""
        return (
            self.client.table("medstudy_lectures")
            .select("id,subject_id,lecture_no,title")
            .execute()
            .data
            or []
        )

    def quiz_sets(self, lecture_id):
        return (
            self.client.table("medstudy_quiz_sets")
            .select("lecture_id,set_no,content,updated_at")
            .eq("lecture_id", lecture_id)
            .order("set_no")
            .execute()
            .data
            or []
        )

    def progress(self, lecture_id):
        rows = (
            self.client.table("medstudy_quiz_progress")
            .select("*")
            .eq("lecture_id", lecture_id)
            .execute()
            .data
            or []
        )
        return {row["question_key"]: row for row in rows}

    def global_review_queue(self, today=None):
        """모든 과목·강의를 통틀어 오늘 복습할 문제와 오답 기록을 가져온다.

        로컬 앱의 `database.get_global_review_queue`와 같은 기준(오늘
        마감인 문제 또는 한 번이라도 틀린 문제)으로 클라우드(Supabase)
        진행률을 모아, 강의·과목 정보를 붙여 돌려준다. 문제 원문(선택지
        포함)은 각 강의의 정리본에만 있으므로, 실제 문항 재구성은
        호출하는 쪽에서 question_key로 맞춰본다.
        """
        today = (today or date.today()).isoformat()

        rows = (
            self.client.table("medstudy_quiz_progress")
            .select("*")
            .or_(f"due_date.lte.{today},incorrect_count.gte.1")
            .execute()
            .data
            or []
        )
        if not rows:
            return []

        lectures_by_id = {row["id"]: row for row in self.all_lectures()}
        subjects_by_id = {row["id"]: row["name"] for row in self.subjects()}

        queue_rows = []
        for row in rows:
            lecture = lectures_by_id.get(row["lecture_id"])
            if not lecture:
                continue
            queue_rows.append({
                **row,
                "lecture_no": lecture["lecture_no"],
                "lecture_title": lecture["title"],
                "subject_id": lecture["subject_id"],
                "subject_name": subjects_by_id.get(lecture["subject_id"], ""),
            })

        queue_rows.sort(
            key=lambda row: (row["due_date"] is None, row["due_date"] or "")
        )
        return queue_rows

    def save_result(self, lecture_id, item, is_correct, previous_progress=None):
        """정답 제출을 채점하고 저장한다.

        `previous_progress`를 넘기면(이미 화면을 그리며 한 번 읽어 둔
        진행률 딕셔너리) 맞힌/틀린 횟수 계산에 그 값을 그대로 쓰고, 이
        함수 안에서 또 한 번 전체 진행률을 네트워크로 다시 읽어오지
        않는다. 문제를 하나 채점할 때마다 왕복이 두 번씩(카운트 조회 +
        저장) 발생해 채점이 느리게 느껴지던 원인이었다.
        """
        if previous_progress is None:
            previous_progress = self.progress(lecture_id)
        previous = previous_progress.get(item["question_key"], {})
        correct_count = int(previous.get("correct_count", 0)) + int(is_correct)
        incorrect_count = int(previous.get("incorrect_count", 0)) + int(not is_correct)
        answer = f'{item["correct_choice"]}. {item["answer_text"]}'
        if item.get("explanation"):
            answer += f' | 해설: {item["explanation"]}'
        schedule = compute_next_review(previous, is_correct)
        row = {
            "lecture_id": lecture_id,
            "question_key": item["question_key"],
            "question": item["question"],
            "answer": answer,
            "incorrect_count": incorrect_count,
            "correct_count": correct_count,
            "review_status": schedule["review_status"],
            "last_result": "correct" if is_correct else "incorrect",
            "repetitions": schedule["repetitions"],
            "ease_factor": schedule["ease_factor"],
            "interval_days": schedule["interval_days"],
            "due_date": schedule["due_date"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        (
            self.client.table("medstudy_quiz_progress")
            .upsert(row, on_conflict="lecture_id,question_key")
            .execute()
        )

    def reset_progress(self, lecture_id):
        (
            self.client.table("medstudy_quiz_progress")
            .delete()
            .eq("lecture_id", lecture_id)
            .execute()
        )

    def signed_pdf_url(self, path, expires_in=3600):
        if not path:
            return None
        result = (
            self.client.storage.from_(self.bucket)
            .create_signed_url(path, expires_in)
        )
        if isinstance(result, str):
            return result
        return result.get("signedURL") or result.get("signedUrl")
