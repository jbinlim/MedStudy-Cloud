import json
import sqlite3
from pathlib import Path

from spaced_repetition import compute_next_review


# =========================================================
# 데이터베이스 경로
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "medstudy.db"


# =========================================================
# DB 연결
# =========================================================

def get_connection():
    # AI 작업과 웹 화면이 동시에 DB를 사용할 때 짧은 잠금 때문에
    # 바로 실패하지 않고 안전하게 기다리도록 한다.
    conn = sqlite3.connect(DB_PATH, timeout=30)

    # SELECT 결과를 dictionary처럼 사용할 수 있게 함
    conn.row_factory = sqlite3.Row

    # Foreign Key 기능 활성화
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")

    return conn


# =========================================================
# DB 초기화
# =========================================================

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # WAL은 읽기 화면과 AI 결과 저장이 겹칠 때 서로 막는 시간을 줄인다.
    cursor.execute("PRAGMA journal_mode = WAL")

    # -------------------------
    # 과목 테이블
    # -------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """
    )

    # -------------------------
    # 강의 테이블
    # -------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS lectures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL,
            lecture_no INTEGER NOT NULL,
            title TEXT,
            pdf_path TEXT,
            transcript_path TEXT,
            audio_path TEXT,
            corrected_transcript_path TEXT,

            FOREIGN KEY (subject_id)
                REFERENCES subjects (id)
                ON DELETE CASCADE,

            UNIQUE(subject_id, lecture_no)
        )
        """
    )

    # -------------------------
    # 예전 DB 호환
    # pdf_path 컬럼이 없다면 추가
    # -------------------------

    cursor.execute(
        "PRAGMA table_info(lectures)"
    )

    columns = [
        row["name"]
        for row in cursor.fetchall()
    ]

    if "pdf_path" not in columns:
        cursor.execute(
            """
            ALTER TABLE lectures
            ADD COLUMN pdf_path TEXT
            """
        )

    if "transcript_path" not in columns:
        cursor.execute(
            """
            ALTER TABLE lectures
            ADD COLUMN transcript_path TEXT
            """
        )

    if "audio_path" not in columns:
        cursor.execute(
            """
            ALTER TABLE lectures
            ADD COLUMN audio_path TEXT
            """
        )

    if "corrected_transcript_path" not in columns:
        cursor.execute(
            """
            ALTER TABLE lectures
            ADD COLUMN corrected_transcript_path TEXT
            """
        )

    # -------------------------
    # PDF 페이지별 텍스트
    # -------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS lecture_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lecture_id INTEGER NOT NULL,
            page_no INTEGER NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            embedded_text TEXT NOT NULL DEFAULT '',
            ocr_text TEXT NOT NULL DEFAULT '',
            image_path TEXT,
            include_in_summary INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY (lecture_id)
                REFERENCES lectures (id)
                ON DELETE CASCADE,

            UNIQUE(lecture_id, page_no)
        )
        """
    )

    cursor.execute(
        "PRAGMA table_info(lecture_pages)"
    )

    page_columns = [
        row["name"]
        for row in cursor.fetchall()
    ]

    for column_name, column_type in (
        ("embedded_text", "TEXT NOT NULL DEFAULT ''"),
        ("ocr_text", "TEXT NOT NULL DEFAULT ''"),
        ("image_path", "TEXT"),
        ("include_in_summary", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column_name not in page_columns:
            cursor.execute(
                f"""
                ALTER TABLE lecture_pages
                ADD COLUMN {column_name} {column_type}
                """
            )

    # -------------------------
    # AI 부분 분석과 최종 정리본
    # -------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS lecture_analysis_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lecture_id INTEGER NOT NULL,
            chunk_no INTEGER NOT NULL,
            source_label TEXT NOT NULL,
            analysis_text TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (lecture_id)
                REFERENCES lectures (id)
                ON DELETE CASCADE,

            UNIQUE(lecture_id, chunk_no)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS lecture_summaries (
            lecture_id INTEGER PRIMARY KEY,
            content TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (lecture_id)
                REFERENCES lectures (id)
                ON DELETE CASCADE
        )
        """
    )

    # 과목 공통 기출 PDF. 강의가 아니라 과목에 귀속시켜 여러 강의에서
    # 같은 기출 근거를 함께 사용할 수 있게 한다.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS subject_exam_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL,
            source_path TEXT NOT NULL,
            local_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            extracted_text TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (subject_id)
                REFERENCES subjects (id)
                ON DELETE CASCADE,

            UNIQUE(subject_id, source_path)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS lecture_figures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lecture_id INTEGER NOT NULL,
            figure_key TEXT NOT NULL,
            page_no INTEGER NOT NULL,
            image_path TEXT NOT NULL,
            caption TEXT NOT NULL,
            x0 REAL NOT NULL,
            y0 REAL NOT NULL,
            x1 REAL NOT NULL,
            y1 REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (lecture_id)
                REFERENCES lectures (id)
                ON DELETE CASCADE,

            UNIQUE(lecture_id, figure_key)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS lecture_exam_figures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lecture_id INTEGER NOT NULL,
            figure_key TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_label TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_order INTEGER NOT NULL,
            page_no INTEGER NOT NULL,
            question_no TEXT,
            image_path TEXT NOT NULL,
            extracted_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (lecture_id)
                REFERENCES lectures (id)
                ON DELETE CASCADE,

            UNIQUE(lecture_id, figure_key)
        )
        """
    )

    # -------------------------
    # 강의별 복습 퀴즈 기록
    # -------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS quiz_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lecture_id INTEGER NOT NULL,
            question_key TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            incorrect_count INTEGER NOT NULL DEFAULT 0,
            correct_count INTEGER NOT NULL DEFAULT 0,
            review_status TEXT NOT NULL DEFAULT 'unseen',
            last_result TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (lecture_id)
                REFERENCES lectures (id)
                ON DELETE CASCADE,

            UNIQUE(lecture_id, question_key)
        )
        """
    )

    # -------------------------
    # 예전 DB 호환
    # 간격 반복(SM-2) 스케줄링 컬럼이 없다면 추가
    # -------------------------

    cursor.execute(
        "PRAGMA table_info(quiz_progress)"
    )

    quiz_progress_columns = [
        row["name"]
        for row in cursor.fetchall()
    ]

    for column_name, column_type in (
        ("repetitions", "INTEGER NOT NULL DEFAULT 0"),
        ("ease_factor", "REAL NOT NULL DEFAULT 2.5"),
        ("interval_days", "INTEGER NOT NULL DEFAULT 0"),
        ("due_date", "TEXT"),
    ):
        if column_name not in quiz_progress_columns:
            cursor.execute(
                f"""
                ALTER TABLE quiz_progress
                ADD COLUMN {column_name} {column_type}
                """
            )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS additional_quiz_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lecture_id INTEGER NOT NULL,
            set_no INTEGER NOT NULL,
            content TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (lecture_id)
                REFERENCES lectures (id)
                ON DELETE CASCADE,

            UNIQUE(lecture_id, set_no)
        )
        """
    )

    # -------------------------
    # 백그라운드 AI 정리 작업
    # -------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS generation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lecture_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            progress REAL NOT NULL DEFAULT 0,
            stage TEXT NOT NULL DEFAULT '대기',
            message TEXT NOT NULL DEFAULT '',
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT,

            FOREIGN KEY (lecture_id)
                REFERENCES lectures (id)
                ON DELETE CASCADE
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS drive_sync_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidates_json TEXT NOT NULL,
            data_dir TEXT NOT NULL,
            auto_generate INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            progress REAL NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '동기화 대기 중',
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )

    # 기존 데이터에는 영향을 주지 않고 자주 사용하는 조회만 빠르게 한다.
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lectures_subject_order
        ON lectures (subject_id, lecture_no)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_figures_lecture_page
        ON lecture_figures (lecture_id, page_no, figure_key)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_exam_figures_lecture_order
        ON lecture_exam_figures (lecture_id, source_order, page_no, id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_quiz_progress_lecture_order
        ON quiz_progress (lecture_id, id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_quiz_progress_due_date
        ON quiz_progress (due_date)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_generation_jobs_status_order
        ON generation_jobs (status, id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_subject_exam_files_subject
        ON subject_exam_files (subject_id, id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_drive_sync_jobs_status_order
        ON drive_sync_jobs (status, id)
        """
    )

    conn.commit()
    conn.close()


# =========================================================
# 과목 추가
# =========================================================

def add_subject(name):
    name = name.strip()

    if not name:
        return False

    conn = get_connection()

    try:
        conn.execute(
            """
            INSERT INTO subjects (name)
            VALUES (?)
            """,
            (name,),
        )

        conn.commit()

        return True

    except sqlite3.IntegrityError:
        return False

    finally:
        conn.close()


# =========================================================
# 과목 목록 가져오기
# =========================================================

def get_subjects():
    conn = get_connection()

    subjects = conn.execute(
        """
        SELECT *
        FROM subjects
        ORDER BY name
        """
    ).fetchall()

    conn.close()

    return subjects


# =========================================================
# 강의 추가
# =========================================================

def add_lecture(
    subject_id,
    lecture_no,
    title="",
):
    conn = get_connection()

    try:
        conn.execute(
            """
            INSERT INTO lectures (
                subject_id,
                lecture_no,
                title
            )
            VALUES (?, ?, ?)
            """,
            (
                subject_id,
                lecture_no,
                title.strip(),
            ),
        )

        conn.commit()

        return True

    except sqlite3.IntegrityError:
        return False

    finally:
        conn.close()


# =========================================================
# 특정 과목의 강의 목록
# =========================================================

def get_lectures(subject_id):
    conn = get_connection()

    lectures = conn.execute(
        """
        SELECT *
        FROM lectures
        WHERE subject_id = ?
        ORDER BY lecture_no
        """,
        (subject_id,),
    ).fetchall()

    conn.close()

    return lectures


# =========================================================
# 특정 강의 하나 가져오기
# =========================================================

def get_lecture_by_id(lecture_id):
    conn = get_connection()

    lecture = conn.execute(
        """
        SELECT *
        FROM lectures
        WHERE id = ?
        """,
        (lecture_id,),
    ).fetchone()

    conn.close()

    return lecture


def update_lecture_title(lecture_id, title):
    """강의의 표시 제목만 변경한다."""
    conn = get_connection()
    conn.execute(
        "UPDATE lectures SET title = ? WHERE id = ?",
        (title.strip(), lecture_id),
    )
    conn.commit()
    conn.close()


# =========================================================
# 강의 삭제
# =========================================================

def delete_lecture(lecture_id):
    conn = get_connection()

    conn.execute(
        """
        DELETE FROM lectures
        WHERE id = ?
        """,
        (lecture_id,),
    )

    conn.commit()
    conn.close()


# =========================================================
# 강의 PDF 경로 저장
# =========================================================

def update_lecture_pdf(
    lecture_id,
    pdf_path,
):
    conn = get_connection()

    conn.execute(
        """
        UPDATE lectures
        SET pdf_path = ?
        WHERE id = ?
        """,
        (
            pdf_path,
            lecture_id,
        ),
    )

    conn.commit()
    conn.close()


# =========================================================
# 강의 전사문 경로 저장
# =========================================================

def update_lecture_transcript(
    lecture_id,
    transcript_path,
):
    conn = get_connection()

    conn.execute(
        """
        UPDATE lectures
        SET transcript_path = ?
        WHERE id = ?
        """,
        (
            transcript_path,
            lecture_id,
        ),
    )

    conn.commit()
    conn.close()


# =========================================================
# 강의 전사문 AI 교정본 경로 저장
# =========================================================

def update_corrected_transcript(
    lecture_id,
    corrected_transcript_path,
):
    conn = get_connection()

    conn.execute(
        """
        UPDATE lectures
        SET corrected_transcript_path = ?
        WHERE id = ?
        """,
        (
            corrected_transcript_path,
            lecture_id,
        ),
    )

    conn.commit()
    conn.close()


# =========================================================
# PDF 페이지별 텍스트 교체
# =========================================================

def replace_lecture_pages(
    lecture_id,
    page_texts,
):
    conn = get_connection()

    try:
        conn.execute(
            """
            DELETE FROM lecture_figures
            WHERE lecture_id = ?
            """,
            (lecture_id,),
        )

        conn.execute(
            """
            DELETE FROM lecture_pages
            WHERE lecture_id = ?
            """,
            (lecture_id,),
        )

        normalized_pages = []

        for page_no, page_data in enumerate(
            page_texts,
            start=1,
        ):
            if isinstance(page_data, str):
                normalized_pages.append(
                    (
                        lecture_id,
                        page_no,
                        page_data,
                        page_data,
                        "",
                        None,
                    )
                )
            else:
                normalized_pages.append(
                    (
                        lecture_id,
                        page_no,
                        page_data.get("text", ""),
                        page_data.get("embedded_text", ""),
                        page_data.get("ocr_text", ""),
                        page_data.get("image_path"),
                    )
                )

        conn.executemany(
            """
            INSERT INTO lecture_pages (
                lecture_id,
                page_no,
                text,
                embedded_text,
                ocr_text,
                image_path
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            normalized_pages,
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =========================================================
# PDF 페이지별 텍스트 가져오기
# =========================================================

def get_lecture_pages(lecture_id):
    conn = get_connection()

    pages = conn.execute(
        """
        SELECT
            page_no,
            text,
            embedded_text,
            ocr_text,
            image_path
            , include_in_summary
        FROM lecture_pages
        WHERE lecture_id = ?
        ORDER BY page_no
        """,
        (lecture_id,),
    ).fetchall()

    conn.close()

    return pages


def update_summary_image_pages(
    lecture_id,
    page_numbers,
):
    conn = get_connection()

    try:
        conn.execute(
            """
            UPDATE lecture_pages
            SET include_in_summary = 0
            WHERE lecture_id = ?
            """,
            (lecture_id,),
        )

        conn.executemany(
            """
            UPDATE lecture_pages
            SET include_in_summary = 1
            WHERE lecture_id = ?
              AND page_no = ?
            """,
            [
                (lecture_id, page_no)
                for page_no in page_numbers
            ],
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def replace_lecture_figures(
    lecture_id,
    figures,
):
    conn = get_connection()

    try:
        conn.execute(
            """
            DELETE FROM lecture_figures
            WHERE lecture_id = ?
            """,
            (lecture_id,),
        )

        conn.executemany(
            """
            INSERT INTO lecture_figures (
                lecture_id,
                figure_key,
                page_no,
                image_path,
                caption,
                x0,
                y0,
                x1,
                y1
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    lecture_id,
                    figure["figure_key"],
                    figure["page_no"],
                    figure["image_path"],
                    figure["caption"],
                    figure["x0"],
                    figure["y0"],
                    figure["x1"],
                    figure["y1"],
                )
                for figure in figures
            ],
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def get_lecture_figures(lecture_id):
    conn = get_connection()

    figures = conn.execute(
        """
        SELECT *
        FROM lecture_figures
        WHERE lecture_id = ?
        ORDER BY page_no, figure_key
        """,
        (lecture_id,),
    ).fetchall()

    conn.close()

    return figures


def replace_lecture_exam_figures(lecture_id, figures):
    """강의에 연결된 기출 원문 크롭을 한 번에 교체한다."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM lecture_exam_figures WHERE lecture_id = ?",
            (lecture_id,),
        )
        conn.executemany(
            """
            INSERT INTO lecture_exam_figures (
                lecture_id, figure_key, source_kind, source_label,
                source_path, source_order, page_no, question_no,
                image_path, extracted_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    lecture_id,
                    figure["figure_key"],
                    figure["source_kind"],
                    figure["source_label"],
                    figure["source_path"],
                    figure["source_order"],
                    figure["page_no"],
                    figure.get("question_no"),
                    figure["image_path"],
                    figure.get("extracted_text", ""),
                )
                for figure in figures
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_lecture_exam_figures(lecture_id):
    conn = get_connection()
    figures = conn.execute(
        """
        SELECT lecture_exam_figures.*
        FROM lecture_exam_figures
        JOIN lectures ON lectures.id = lecture_exam_figures.lecture_id
        WHERE lecture_exam_figures.lecture_id = ?
          AND lecture_exam_figures.source_kind = 'lecture'
          AND lecture_exam_figures.source_path = lectures.pdf_path
        ORDER BY lecture_exam_figures.source_order,
                 lecture_exam_figures.page_no,
                 lecture_exam_figures.id
        """,
        (lecture_id,),
    ).fetchall()
    conn.close()
    return figures


# =========================================================
# AI 분석 결과와 최종 정리본 저장
# =========================================================

def save_lecture_ai_results(
    lecture_id,
    analyses,
    summary,
    model,
):
    conn = get_connection()

    try:
        conn.execute(
            """
            DELETE FROM lecture_analysis_chunks
            WHERE lecture_id = ?
            """,
            (lecture_id,),
        )

        conn.executemany(
            """
            INSERT INTO lecture_analysis_chunks (
                lecture_id,
                chunk_no,
                source_label,
                analysis_text,
                model
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    lecture_id,
                    chunk_no,
                    analysis["source_label"],
                    analysis["analysis_text"],
                    model,
                )
                for chunk_no, analysis
                in enumerate(analyses, start=1)
            ],
        )

        conn.execute(
            """
            INSERT INTO lecture_summaries (
                lecture_id,
                content,
                model,
                created_at
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(lecture_id) DO UPDATE SET
                content = excluded.content,
                model = excluded.model,
                created_at = CURRENT_TIMESTAMP
            """,
            (
                lecture_id,
                summary,
                model,
            ),
        )

        # 정리본 입력(강의록·전사문·기출)이 바뀌면 웹 기본 문제도 다음
        # 접속 때 새 정리본을 기준으로 다시 만들게 한다. 사용자가 추가로
        # 생성한 문제 세트와 오답/복습 기록은 보존한다.
        conn.execute(
            """
            DELETE FROM additional_quiz_sets
            WHERE lecture_id = ? AND set_no = 0
            """,
            (lecture_id,),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =========================================================
# 저장된 AI 최종 정리본 가져오기
# =========================================================

def get_lecture_summary(lecture_id):
    conn = get_connection()

    summary = conn.execute(
        """
        SELECT content, model, created_at
        FROM lecture_summaries
        WHERE lecture_id = ?
        """,
        (lecture_id,),
    ).fetchone()

    conn.close()

    return summary


def get_recent_lecture_summaries(limit=5):
    """최근 저장된 정리본을 생성 시각 역순으로 가져온다."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            lecture_summaries.lecture_id,
            lecture_summaries.created_at,
            lecture_summaries.model,
            lectures.lecture_no,
            lectures.title AS lecture_title,
            subjects.id AS subject_id,
            subjects.name AS subject_name
        FROM lecture_summaries
        JOIN lectures ON lectures.id = lecture_summaries.lecture_id
        JOIN subjects ON subjects.id = lectures.subject_id
        ORDER BY lecture_summaries.created_at DESC,
                 lecture_summaries.lecture_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def get_all_lecture_summaries():
    """저장된 모든 정리본과 강의 정보를 생성 시각 역순으로 가져온다."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            lecture_summaries.lecture_id,
            lecture_summaries.created_at,
            lecture_summaries.model,
            lectures.lecture_no,
            lectures.title AS lecture_title,
            subjects.id AS subject_id,
            subjects.name AS subject_name
        FROM lecture_summaries
        JOIN lectures ON lectures.id = lecture_summaries.lecture_id
        JOIN subjects ON subjects.id = lectures.subject_id
        ORDER BY lecture_summaries.created_at DESC,
                 lecture_summaries.lecture_id DESC
        """
    ).fetchall()
    conn.close()
    return rows


def upsert_subject_exam_file(
    subject_id,
    source_path,
    local_path,
    file_name,
    file_hash,
    extracted_text,
):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO subject_exam_files (
            subject_id, source_path, local_path, file_name,
            file_hash, extracted_text, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(subject_id, source_path) DO UPDATE SET
            local_path = excluded.local_path,
            file_name = excluded.file_name,
            file_hash = excluded.file_hash,
            extracted_text = excluded.extracted_text,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            subject_id,
            source_path,
            local_path,
            file_name,
            file_hash,
            extracted_text,
        ),
    )
    conn.commit()
    conn.close()


def get_subject_exam_files(subject_id):
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM subject_exam_files
        WHERE subject_id = ?
        ORDER BY file_name, id
        """,
        (subject_id,),
    ).fetchall()
    conn.close()
    return rows


def get_lecture_exam_files(lecture_id):
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT subject_exam_files.*
        FROM subject_exam_files
        JOIN lectures
          ON lectures.subject_id = subject_exam_files.subject_id
        WHERE lectures.id = ?
        ORDER BY subject_exam_files.file_name, subject_exam_files.id
        """,
        (lecture_id,),
    ).fetchall()
    conn.close()
    return rows


def update_lecture_summary_content(
    lecture_id,
    content,
    model,
):
    conn = get_connection()

    conn.execute(
        """
        UPDATE lecture_summaries
        SET content = ?,
            model = ?,
            created_at = CURRENT_TIMESTAMP
        WHERE lecture_id = ?
        """,
        (
            content,
            model,
            lecture_id,
        ),
    )

    conn.commit()
    conn.close()


# =========================================================
# 복습 퀴즈 진행 상황
# =========================================================

def get_quiz_progress(lecture_id):
    conn = get_connection()

    rows = conn.execute(
        """
        SELECT *
        FROM quiz_progress
        WHERE lecture_id = ?
        ORDER BY id
        """,
        (lecture_id,),
    ).fetchall()

    conn.close()

    return {
        row["question_key"]: row
        for row in rows
    }


def save_quiz_result(
    lecture_id,
    question_key,
    question,
    answer,
    is_correct,
):
    conn = get_connection()

    previous_row = conn.execute(
        """
        SELECT correct_count, incorrect_count,
               repetitions, ease_factor, interval_days
        FROM quiz_progress
        WHERE lecture_id = ? AND question_key = ?
        """,
        (lecture_id, question_key),
    ).fetchone()
    previous = dict(previous_row) if previous_row else None

    correct_count = (previous["correct_count"] if previous else 0) + int(
        is_correct
    )
    incorrect_count = (previous["incorrect_count"] if previous else 0) + int(
        not is_correct
    )
    last_result = "correct" if is_correct else "incorrect"

    schedule = compute_next_review(previous, is_correct)

    conn.execute(
        """
        INSERT INTO quiz_progress (
            lecture_id,
            question_key,
            question,
            answer,
            incorrect_count,
            correct_count,
            review_status,
            last_result,
            repetitions,
            ease_factor,
            interval_days,
            due_date,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            lecture_id,
            question_key,
            question,
            answer,
            incorrect_count,
            correct_count,
            schedule["review_status"],
            last_result,
            schedule["repetitions"],
            schedule["ease_factor"],
            schedule["interval_days"],
            schedule["due_date"],
        ),
    )

    conn.commit()
    conn.close()


def reset_quiz_progress(lecture_id):
    conn = get_connection()

    conn.execute(
        """
        DELETE FROM quiz_progress
        WHERE lecture_id = ?
        """,
        (lecture_id,),
    )

    conn.commit()
    conn.close()


def get_global_review_queue(today=None):
    """모든 과목·강의를 통틀어 오늘 복습할 문제와 오답 기록을 가져온다.

    문제 원문(선택지 포함)은 각 강의의 정리본에만 있으므로, 여기서는
    강의·과목 정보가 붙은 quiz_progress 행만 돌려주고, 실제 문항
    재구성은 호출하는 쪽에서 question_key로 맞춰본다.
    """
    from datetime import date

    today = (today or date.today()).isoformat()

    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            quiz_progress.*,
            lectures.lecture_no,
            lectures.title AS lecture_title,
            subjects.id AS subject_id,
            subjects.name AS subject_name
        FROM quiz_progress
        JOIN lectures ON lectures.id = quiz_progress.lecture_id
        JOIN subjects ON subjects.id = lectures.subject_id
        WHERE
            (quiz_progress.due_date IS NOT NULL AND quiz_progress.due_date <= ?)
            OR quiz_progress.incorrect_count >= 1
        ORDER BY
            (quiz_progress.due_date IS NULL),
            quiz_progress.due_date
        """,
        (today,),
    ).fetchall()
    conn.close()

    return rows


def get_subject_progress_overview(today=None):
    """과목별 강의 정리 진도와 퀴즈 성취도를 한 번에 집계한다.

    퀴즈 성취도는 최소 한 번 이상 풀어본 문제(quiz_progress에 행이
    있는 문제) 기준이다. 아직 한 번도 안 푼 문제는 정리본 마크다운을
    과목 전체에 걸쳐 다시 파싱해야만 셀 수 있어 비용이 크므로, 이
    대시보드에서는 "생성된 강의 수"로 진도를, "푼 문제 중 학습 완료
    비율"로 성취도를 나타낸다.
    """
    from datetime import date

    today = (today or date.today()).isoformat()

    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            subjects.id AS subject_id,
            subjects.name AS subject_name,
            COUNT(DISTINCT lectures.id) AS lecture_count,
            COUNT(DISTINCT lecture_summaries.lecture_id) AS summarized_count,
            COUNT(DISTINCT quiz_progress.id) AS answered_count,
            COALESCE(SUM(
                CASE WHEN quiz_progress.review_status = 'mastered'
                THEN 1 ELSE 0 END
            ), 0) AS mastered_count,
            COALESCE(SUM(
                CASE WHEN quiz_progress.review_status = 'review'
                THEN 1 ELSE 0 END
            ), 0) AS review_count,
            COALESCE(SUM(
                CASE WHEN quiz_progress.due_date IS NOT NULL
                     AND quiz_progress.due_date <= ?
                THEN 1 ELSE 0 END
            ), 0) AS due_count
        FROM subjects
        LEFT JOIN lectures ON lectures.subject_id = subjects.id
        LEFT JOIN lecture_summaries
            ON lecture_summaries.lecture_id = lectures.id
        LEFT JOIN quiz_progress ON quiz_progress.lecture_id = lectures.id
        GROUP BY subjects.id, subjects.name
        ORDER BY subjects.name
        """,
        (today,),
    ).fetchall()
    conn.close()

    return rows


def get_additional_quiz_sets(lecture_id):
    conn = get_connection()

    rows = conn.execute(
        """
        SELECT *
        FROM additional_quiz_sets
        WHERE lecture_id = ? AND set_no > 0
        ORDER BY set_no
        """,
        (lecture_id,),
    ).fetchall()

    conn.close()
    return rows


def get_base_web_quiz(lecture_id):
    conn = get_connection()

    row = conn.execute(
        """
        SELECT *
        FROM additional_quiz_sets
        WHERE lecture_id = ? AND set_no = 0
        """,
        (lecture_id,),
    ).fetchone()

    conn.close()
    return row


def save_base_web_quiz(
    lecture_id,
    content,
    model,
):
    conn = get_connection()

    conn.execute(
        """
        INSERT INTO additional_quiz_sets (
            lecture_id,
            set_no,
            content,
            model
        )
        VALUES (?, 0, ?, ?)
        ON CONFLICT(lecture_id, set_no) DO UPDATE SET
            content = excluded.content,
            model = excluded.model,
            created_at = CURRENT_TIMESTAMP
        """,
        (
            lecture_id,
            content,
            model,
        ),
    )

    conn.commit()
    conn.close()


def add_additional_quiz_set(
    lecture_id,
    content,
    model,
):
    conn = get_connection()

    try:
        # 동시에 문제 세트를 추가해도 같은 번호를 계산하지 않게 잠근다.
        conn.execute("BEGIN IMMEDIATE")

        next_set_no = conn.execute(
            """
            SELECT COALESCE(MAX(set_no), 0) + 1
            FROM additional_quiz_sets
            WHERE lecture_id = ?
            """,
            (lecture_id,),
        ).fetchone()[0]

        conn.execute(
            """
            INSERT INTO additional_quiz_sets (
                lecture_id,
                set_no,
                content,
                model
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                lecture_id,
                next_set_no,
                content,
                model,
            ),
        )

        conn.commit()
        return next_set_no
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =========================================================
# 백그라운드 AI 정리 작업
# =========================================================

def enqueue_generation_job(lecture_id):
    """같은 강의의 대기/실행 작업이 없을 때만 새 작업을 등록한다."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT id
            FROM generation_jobs
            WHERE lecture_id = ?
              AND status IN ('pending', 'running')
            ORDER BY id DESC
            LIMIT 1
            """,
            (lecture_id,),
        ).fetchone()
        if existing:
            conn.commit()
            return existing["id"], False

        cursor = conn.execute(
            """
            INSERT INTO generation_jobs (lecture_id)
            VALUES (?)
            """,
            (lecture_id,),
        )
        conn.commit()
        return cursor.lastrowid, True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_pending_generation_job(job_id):
    """아직 시작하지 않은 생성 작업만 안전하게 취소한다."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            UPDATE generation_jobs
            SET status = 'cancelled',
                stage = '취소',
                message = '사용자가 대기열에서 취소했습니다.',
                error = NULL,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'pending'
            """,
            (job_id,),
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def dismiss_failed_generation_job(job_id):
    """실패 알림만 숨기고 강의 자료와 다른 작업 기록은 보존한다."""
    conn = get_connection()
    cursor = conn.execute(
        """
        UPDATE generation_jobs
        SET status = 'dismissed',
            message = '사용자가 실패 알림을 숨겼습니다.'
        WHERE id = ? AND status = 'failed'
        """,
        (job_id,),
    )
    conn.commit()
    changed = cursor.rowcount == 1
    conn.close()
    return changed


def reset_interrupted_generation_jobs():
    """작업 프로세스가 비정상 종료된 실행 항목을 다시 대기 상태로 돌린다."""
    conn = get_connection()
    conn.execute(
        """
        UPDATE generation_jobs
        SET status = 'pending',
            stage = '재시작 대기',
            message = '이전 작업 프로세스가 종료되어 다시 시작합니다.',
            started_at = NULL
        WHERE status = 'running'
        """
    )
    conn.commit()
    conn.close()


def claim_next_generation_job():
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute(
            """
            SELECT id
            FROM generation_jobs
            WHERE status = 'pending'
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
        if not job:
            conn.commit()
            return None

        conn.execute(
            """
            UPDATE generation_jobs
            SET status = 'running',
                progress = 0,
                stage = '시작',
                message = '생성 작업을 시작합니다.',
                error = NULL,
                started_at = CURRENT_TIMESTAMP,
                finished_at = NULL
            WHERE id = ?
            """,
            (job["id"],),
        )
        context = conn.execute(
            """
            SELECT
                generation_jobs.id AS job_id,
                lectures.id AS lecture_id,
                lectures.lecture_no,
                lectures.title AS lecture_title,
                lectures.pdf_path,
                lectures.transcript_path,
                subjects.name AS subject_name
            FROM generation_jobs
            JOIN lectures ON lectures.id = generation_jobs.lecture_id
            JOIN subjects ON subjects.id = lectures.subject_id
            WHERE generation_jobs.id = ?
            """,
            (job["id"],),
        ).fetchone()
        conn.commit()
        return context
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_generation_job(job_id, progress, stage, message):
    conn = get_connection()
    conn.execute(
        """
        UPDATE generation_jobs
        SET progress = ?, stage = ?, message = ?
        WHERE id = ? AND status = 'running'
        """,
        (progress, stage, message, job_id),
    )
    conn.commit()
    conn.close()


def finish_generation_job(job_id, error=None, warnings=None):
    conn = get_connection()
    if error:
        conn.execute(
            """
            UPDATE generation_jobs
            SET status = 'failed',
                stage = '실패',
                message = '정리본 생성에 실패했습니다.',
                error = ?,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(error)[-3000:], job_id),
        )
    else:
        # 자주 출제되는 내용 요약·기출 하이라이트처럼 정리본 생성 자체를
        # 실패로 만들지는 않지만 건너뛴 부가 단계가 있으면, 완료 메시지에
        # 남겨서 나중에 "왜 비어 있지"를 조용히 지나치지 않게 한다.
        message = "AI 정리본 생성이 완료되었습니다."
        if warnings:
            message += " ⚠ 건너뛴 항목: " + " · ".join(warnings)
        conn.execute(
            """
            UPDATE generation_jobs
            SET status = 'completed',
                progress = 1,
                stage = '완료',
                message = ?,
                error = NULL,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (message, job_id),
        )
    conn.commit()
    conn.close()


def get_latest_generation_job(lecture_id):
    """이 강의의 가장 최근 생성 작업을 가져온다(완료 메시지 속 경고 표시용)."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT * FROM generation_jobs
        WHERE lecture_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (lecture_id,),
    ).fetchone()
    conn.close()
    return row


def get_generation_jobs(limit=20):
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            generation_jobs.*,
            lectures.lecture_no,
            lectures.title AS lecture_title,
            subjects.name AS subject_name
        FROM generation_jobs
        JOIN lectures ON lectures.id = generation_jobs.lecture_id
        JOIN subjects ON subjects.id = lectures.subject_id
        ORDER BY generation_jobs.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def enqueue_drive_sync_job(candidates, data_dir, auto_generate=True):
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            INSERT INTO drive_sync_jobs (
                candidates_json, data_dir, auto_generate
            ) VALUES (?, ?, ?)
            """,
            (
                json.dumps(candidates, ensure_ascii=False),
                str(data_dir),
                int(auto_generate),
            ),
        )
        conn.commit()
        return cursor.lastrowid, True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset_interrupted_drive_sync_jobs():
    conn = get_connection()
    conn.execute(
        """
        UPDATE drive_sync_jobs
        SET status = 'pending', progress = 0,
            message = '이전 동기화가 중단되어 다시 시작합니다.',
            started_at = NULL
        WHERE status = 'running'
        """
    )
    conn.commit()
    conn.close()


def claim_next_drive_sync_job():
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM drive_sync_jobs
            WHERE status = 'pending' ORDER BY id LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE drive_sync_jobs
            SET status = 'running', progress = 0.05,
                message = 'Google Drive 자료를 복사하고 분석하고 있습니다.',
                error = NULL, started_at = CURRENT_TIMESTAMP,
                finished_at = NULL
            WHERE id = ?
            """,
            (row["id"],),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_drive_sync_job(job_id, result=None, error=None):
    conn = get_connection()
    if error:
        conn.execute(
            """
            UPDATE drive_sync_jobs
            SET status = 'failed', message = '동기화에 실패했습니다.',
                error = ?, finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(error)[-3000:], job_id),
        )
    else:
        conn.execute(
            """
            UPDATE drive_sync_jobs
            SET status = 'completed', progress = 1,
                message = '동기화와 생성 대기열 등록이 완료되었습니다.',
                result_json = ?, error = NULL,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (json.dumps(result or {}, ensure_ascii=False), job_id),
        )
    conn.commit()
    conn.close()


def get_drive_sync_jobs(limit=5):
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM drive_sync_jobs ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows
