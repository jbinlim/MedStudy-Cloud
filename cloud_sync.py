"""Mac의 완성된 학습 자료를 MedStudy 클라우드 학습 앱으로 복제한다."""

import os
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from ai_service import split_summary_sections
from database import (
    DB_PATH,
    get_lecture_figures,
    get_lecture_exam_figures,
    get_lecture_pages,
)
from pdf_export import create_summary_pdf
from quiz_utils import extract_review_quiz
from spaced_repetition import DEFAULT_EASE_FACTOR


BASE_DIR = Path(__file__).resolve().parent


def _settings():
    url = os.getenv("MEDSTUDY_SUPABASE_URL", "")
    key = os.getenv("MEDSTUDY_SUPABASE_SERVICE_KEY", "")
    bucket = os.getenv("MEDSTUDY_SUPABASE_BUCKET", "medstudy")
    secrets_path = BASE_DIR / ".streamlit" / "secrets.toml"
    if (not url or not key) and secrets_path.exists():
        data = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
        values = data.get("supabase", {})
        url = url or values.get("url", "")
        key = key or values.get("service_key", "")
        bucket = values.get("bucket", bucket)
    if not url or not key:
        return None
    return {"url": url, "key": key, "bucket": bucket}


def cloud_sync_configured():
    return _settings() is not None


def _iso_utc(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _client():
    settings = _settings()
    if not settings:
        raise RuntimeError(
            ".streamlit/secrets.toml에 Supabase 연결 정보를 먼저 설정해주세요."
        )
    try:
        from supabase import create_client
    except ImportError as error:
        raise RuntimeError("supabase 패키지가 설치되어 있지 않습니다.") from error
    return create_client(settings["url"], settings["key"]), settings["bucket"]


def _progress_timestamp(row):
    value = row.get("updated_at") if row else None
    return _iso_utc(value) or "1970-01-01T00:00:00+00:00"


def _save_local_progress_rows(rows):
    if not rows:
        return
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.executemany(
        """
        INSERT INTO quiz_progress (
            lecture_id, question_key, question, answer,
            incorrect_count, correct_count, review_status,
            last_result, repetitions, ease_factor, interval_days,
            due_date, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lecture_id, question_key) DO UPDATE SET
            question = excluded.question,
            answer = excluded.answer,
            incorrect_count = excluded.incorrect_count,
            correct_count = excluded.correct_count,
            review_status = excluded.review_status,
            last_result = excluded.last_result,
            repetitions = excluded.repetitions,
            ease_factor = excluded.ease_factor,
            interval_days = excluded.interval_days,
            due_date = excluded.due_date,
            updated_at = excluded.updated_at
        """,
        [
            (
                row["lecture_id"], row["question_key"], row["question"],
                row["answer"], int(row.get("incorrect_count", 0)),
                int(row.get("correct_count", 0)),
                row.get("review_status", "unseen"), row.get("last_result"),
                int(row.get("repetitions") or 0),
                float(row.get("ease_factor") or DEFAULT_EASE_FACTOR),
                int(row.get("interval_days") or 0),
                row.get("due_date"),
                _iso_utc(row.get("updated_at")),
            )
            for row in rows
        ],
    )
    connection.commit()
    connection.close()


def sync_quiz_progress_bidirectional(lecture_id):
    """Mac과 iPad 중 더 최근에 저장된 문항별 복습 기록을 양쪽에 반영한다."""
    client, _ = _client()

    # `medstudy_quiz_progress.lecture_id`는 `medstudy_lectures.id`를
    # 참조하는 외래키다. 로컬 DB가 재생성되어 강의 id가 바뀌었거나,
    # 강의 생성 직후 자동 클라우드 동기화가 아직 안 됐거나 실패한
    # 상태에서 퀴즈를 풀면, 강의 자체가 클라우드에 없는데 복습 기록만
    # 올리려다 외래키 위반으로 실패한다. 먼저 강의를 동기화해 존재를
    # 보장한 뒤 진행한다.
    remote_lecture = (
        client.table("medstudy_lectures")
        .select("id")
        .eq("id", lecture_id)
        .execute()
        .data
    )
    if not remote_lecture:
        sync_lecture_to_cloud(lecture_id)

    remote_rows = (
        client.table("medstudy_quiz_progress")
        .select("*")
        .eq("lecture_id", lecture_id)
        .execute()
        .data
        or []
    )
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    local_rows = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM quiz_progress WHERE lecture_id = ?",
            (lecture_id,),
        )
    ]
    connection.close()

    local_by_key = {row["question_key"]: row for row in local_rows}
    remote_by_key = {row["question_key"]: row for row in remote_rows}
    local_updates = []
    remote_updates = []

    for question_key in local_by_key.keys() | remote_by_key.keys():
        local = local_by_key.get(question_key)
        remote = remote_by_key.get(question_key)
        if local is None:
            local_updates.append(remote)
        elif remote is None:
            remote_updates.append(local)
        elif _progress_timestamp(remote) > _progress_timestamp(local):
            local_updates.append(remote)
        elif _progress_timestamp(local) > _progress_timestamp(remote):
            remote_updates.append(local)

    _save_local_progress_rows(local_updates)
    if remote_updates:
        payload = [
            {
                "lecture_id": row["lecture_id"],
                "question_key": row["question_key"],
                "question": row["question"],
                "answer": row["answer"],
                "incorrect_count": int(row.get("incorrect_count", 0)),
                "correct_count": int(row.get("correct_count", 0)),
                "review_status": row.get("review_status", "unseen"),
                "last_result": row.get("last_result"),
                "repetitions": int(row.get("repetitions") or 0),
                "ease_factor": float(row.get("ease_factor") or DEFAULT_EASE_FACTOR),
                "interval_days": int(row.get("interval_days") or 0),
                "due_date": row.get("due_date"),
                "updated_at": _iso_utc(row.get("updated_at")),
            }
            for row in remote_updates
        ]
        (
            client.table("medstudy_quiz_progress")
            .upsert(payload, on_conflict="lecture_id,question_key")
            .execute()
        )
    return {
        "downloaded": len(local_updates),
        "uploaded": len(remote_updates),
    }


def try_sync_quiz_progress(lecture_id):
    if not cloud_sync_configured():
        return None
    return sync_quiz_progress_bidirectional(lecture_id)


def _local_lecture(lecture_id):
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT lectures.id, lectures.subject_id, lectures.lecture_no,
               COALESCE(lectures.title, '') AS title,
               subjects.name AS subject_name,
               lecture_summaries.content AS summary_content,
               lecture_summaries.created_at AS summary_updated_at
        FROM lectures
        JOIN subjects ON subjects.id = lectures.subject_id
        JOIN lecture_summaries ON lecture_summaries.lecture_id = lectures.id
        WHERE lectures.id = ?
        """,
        (lecture_id,),
    ).fetchone()
    quiz_rows = connection.execute(
        """
        SELECT set_no, content, created_at
        FROM additional_quiz_sets
        WHERE lecture_id = ?
        ORDER BY set_no
        """,
        (lecture_id,),
    ).fetchall()
    connection.close()
    return row, quiz_rows


def sync_lecture_to_cloud(lecture_id, force=False):
    client, bucket = _client()
    lecture, quiz_rows = _local_lecture(lecture_id)
    if lecture is None:
        raise ValueError("완성된 정리본이 있는 강의를 찾지 못했습니다.")

    client.table("medstudy_subjects").upsert({
        "id": lecture["subject_id"],
        "name": lecture["subject_name"],
    }).execute()

    # `medstudy_lectures`는 (subject_id, lecture_no)에 유니크 제약이 있다.
    # 로컬 DB가 재생성(마이그레이션·재설치 등)되면 같은 강의가 다른
    # id를 받을 수 있는데, 이때 예전 id로 클라우드에 남아있던 행이
    # 그대로 있으면 새 id로 upsert할 때 이 유니크 제약을 위반한다.
    # 같은 자리(subject_id, lecture_no)를 차지한 다른 id의 오래된 행을
    # 먼저 지워, 관련 퀴즈 데이터(on delete cascade)까지 정리한다.
    stale_rows = (
        client.table("medstudy_lectures")
        .select("id")
        .eq("subject_id", lecture["subject_id"])
        .eq("lecture_no", lecture["lecture_no"])
        .neq("id", lecture_id)
        .execute()
        .data
        or []
    )
    if stale_rows:
        client.table("medstudy_lectures").delete().in_(
            "id", [row["id"] for row in stale_rows]
        ).execute()

    updated_at = _iso_utc(lecture["summary_updated_at"])
    remote_rows = (
        client.table("medstudy_lectures")
        .select("summary_updated_at,summary_pdf_path")
        .eq("id", lecture_id)
        .execute()
        .data
        or []
    )
    unchanged = bool(
        remote_rows
        and _iso_utc(remote_rows[0].get("summary_updated_at")) == updated_at
        and remote_rows[0].get("summary_pdf_path")
    )
    pdf_path = f'summaries/{lecture["subject_id"]}/{lecture_id}.pdf'
    exam_pdf_path = f'exams/{lecture["subject_id"]}/{lecture_id}.pdf'
    # 기출이 많은 강의는 "## 4. 기출문제 분석"이 정리본 대부분을 차지해
    # 핵심 개념이 상대적으로 빈약해 보인다. 앱(Streamlit)과 마찬가지로
    # 본문과 기출 분석을 별개 PDF로 나눠 올린다.
    main_content, exam_content = split_summary_sections(lecture["summary_content"])
    if force or not unchanged:
        pages = [dict(row) for row in get_lecture_pages(lecture_id)]
        figures = [dict(row) for row in get_lecture_figures(lecture_id)]
        exam_figures = [dict(row) for row in get_lecture_exam_figures(lecture_id)]
        pdf_bytes = create_summary_pdf(main_content, pages, figures, [])
        client.storage.from_(bucket).upload(
            path=pdf_path,
            file=pdf_bytes,
            file_options={
                "content-type": "application/pdf",
                "cache-control": "3600",
                "upsert": "true",
            },
        )

        if exam_content:
            exam_title = (
                f'# {lecture["subject_name"]} - '
                f'{lecture["lecture_no"]}강 기출 분석\n\n'
            )
            exam_pdf_bytes = create_summary_pdf(
                exam_title + exam_content, [], [], exam_figures
            )
            client.storage.from_(bucket).upload(
                path=exam_pdf_path,
                file=exam_pdf_bytes,
                file_options={
                    "content-type": "application/pdf",
                    "cache-control": "3600",
                    "upsert": "true",
                },
            )

    lecture_payload = {
        "id": lecture_id,
        "subject_id": lecture["subject_id"],
        "lecture_no": lecture["lecture_no"],
        "title": lecture["title"],
        "summary_pdf_path": pdf_path,
        "exam_pdf_path": exam_pdf_path if exam_content else None,
        "summary_updated_at": updated_at,
    }
    try:
        client.table("medstudy_lectures").upsert(lecture_payload).execute()
    except Exception as error:
        # `exam_pdf_path` 컬럼을 추가하는 마이그레이션(cloud/schema.sql
        # 참고)을 아직 Supabase에서 실행하지 않았으면 이 upsert가 매번
        # 실패한다. 그러면 이 강의 행이 계속 "없는" 상태로 남아, 다음
        # 동기화 때마다 "내용이 바뀌었나?" 판단(`unchanged`)까지 매번
        # False가 되어 PDF 2개를 처음부터 다시 만들고 업로드하는 무거운
        # 작업을 반복하게 된다(퀴즈 진행률 동기화가 몇 초씩 걸리던
        # 원인이 바로 이거였다). 컬럼이 없다는 스키마 캐시 오류일 때만
        # 그 필드를 빼고 재시도해, 마이그레이션 전에도 최소한 이
        # 무한 재작업은 막는다.
        message = str(error)
        if "exam_pdf_path" in message and "schema cache" in message:
            lecture_payload.pop("exam_pdf_path", None)
            client.table("medstudy_lectures").upsert(lecture_payload).execute()
        else:
            raise

    quiz_payload = []
    seen_sets = set()
    for row in quiz_rows:
        if not extract_review_quiz(row["content"]):
            continue
        quiz_payload.append({
            "lecture_id": lecture_id,
            "set_no": row["set_no"],
            "content": row["content"],
            "updated_at": _iso_utc(row["created_at"]),
        })
        seen_sets.add(row["set_no"])
    if 0 not in seen_sets and extract_review_quiz(lecture["summary_content"]):
        quiz_payload.insert(0, {
            "lecture_id": lecture_id,
            "set_no": 0,
            "content": lecture["summary_content"],
            "updated_at": updated_at,
        })
    if quiz_payload:
        (
            client.table("medstudy_quiz_sets")
            .upsert(quiz_payload, on_conflict="lecture_id,set_no")
            .execute()
        )
    return {
        "lecture_id": lecture_id,
        "quiz_sets": len(quiz_payload),
        "pdf": pdf_path,
        "exam_pdf": exam_pdf_path if exam_content else None,
    }


def sync_all_to_cloud(force=False):
    connection = sqlite3.connect(DB_PATH)
    lecture_ids = [
        row[0]
        for row in connection.execute(
            "SELECT lecture_id FROM lecture_summaries ORDER BY lecture_id"
        )
    ]
    connection.close()
    return [sync_lecture_to_cloud(lecture_id, force=force) for lecture_id in lecture_ids]


def try_sync_lecture_to_cloud(lecture_id):
    """설정된 경우만 자동 동기화한다. 로컬 생성 성공 여부에는 영향을 주지 않는다."""
    if not cloud_sync_configured():
        return None
    return sync_lecture_to_cloud(lecture_id)


if __name__ == "__main__":
    results = sync_all_to_cloud()
    print(f"클라우드 동기화 완료: {len(results)}개 강의")
