from datetime import datetime, timezone


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
            .select("id,subject_id,lecture_no,title,summary_pdf_path,summary_updated_at")
            .eq("subject_id", subject_id)
            .order("lecture_no")
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

    def save_result(self, lecture_id, item, is_correct):
        previous = self.progress(lecture_id).get(item["question_key"], {})
        correct_count = int(previous.get("correct_count", 0)) + int(is_correct)
        incorrect_count = int(previous.get("incorrect_count", 0)) + int(not is_correct)
        answer = f'{item["correct_choice"]}. {item["answer_text"]}'
        if item.get("explanation"):
            answer += f' | 해설: {item["explanation"]}'
        row = {
            "lecture_id": lecture_id,
            "question_key": item["question_key"],
            "question": item["question"],
            "answer": answer,
            "incorrect_count": incorrect_count,
            "correct_count": correct_count,
            "review_status": "mastered" if is_correct else "review",
            "last_result": "correct" if is_correct else "incorrect",
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
