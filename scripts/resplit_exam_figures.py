"""보라색 박스로 여러 기출 문항이 합쳐진 강의의 기출 이미지를 다시
쪼개서 저장하고, 기출문제 상세 분석도 새 이미지 기준으로 다시 생성한다.

사용법: python3 scripts/resplit_exam_figures.py <lecture_id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database
from exam_figure_service import prepare_exam_figures
from ai_service import (
    EXAM_SECTION_HEADING,
    NEXT_TOP_LEVEL_SECTION,
    build_exam_analysis_request,
    find_missing_exam_figures,
    sanitize_generated_summary,
)
from codex_service import find_codex_cli, generate_summary_with_codex
from cloud_sync import try_sync_lecture_to_cloud

EXAM_ANALYSIS_BATCH_SIZE = 6


def _exam_batch_image_paths(batch, page_image_by_no):
    paths = []
    seen = set()
    for figure in batch:
        for path in (
            page_image_by_no.get(figure.get("page_no")),
            figure["image_path"],
        ):
            if path and path not in seen and Path(path).exists():
                paths.append(path)
                seen.add(path)
    return paths


def main():
    lecture_id = int(sys.argv[1])
    lecture = dict(database.get_lecture_by_id(lecture_id))
    subject = next(
        row for row in database.get_subjects()
        if row["id"] == lecture["subject_id"]
    )
    subject_name = subject["name"]
    lecture_title = f'{lecture["lecture_no"]}강 {lecture["title"]}'.strip()
    pdf_path = Path(lecture["pdf_path"])

    page_records = [dict(row) for row in database.get_lecture_pages(lecture_id)]

    print("기출 이미지 재탐색 중...")
    new_exam_figures = prepare_exam_figures(
        lecture_id,
        pdf_path,
        page_records,
        [],
        pdf_path.parent / "exam_figures",
    )
    print(f"  새 기출 이미지 {len(new_exam_figures)}개")
    database.replace_lecture_exam_figures(lecture_id, new_exam_figures)
    print("DB에 저장 완료.")

    exam_figures = [dict(row) for row in database.get_lecture_exam_figures(lecture_id)]
    page_image_by_no = {
        page["page_no"]: page["image_path"]
        for page in page_records
        if page.get("image_path")
    }

    existing_summary = database.get_lecture_summary(lecture_id)
    summary = existing_summary["content"]

    codex_path = find_codex_cli()
    if not codex_path:
        print("Codex CLI를 찾지 못했습니다.")
        return

    print(f"[{subject_name} - {lecture_title}] 기출 문항 {len(exam_figures)}개 재분석 시작")

    exam_sections = []
    for batch_start in range(0, len(exam_figures), EXAM_ANALYSIS_BATCH_SIZE):
        batch = exam_figures[batch_start:batch_start + EXAM_ANALYSIS_BATCH_SIZE]
        print(f"  배치 {batch_start + len(batch)}/{len(exam_figures)} 요청 중...")
        batch_request = build_exam_analysis_request(subject_name, lecture_title, batch)
        batch_content = generate_summary_with_codex(
            codex_path,
            batch_request,
            image_paths=_exam_batch_image_paths(batch, page_image_by_no),
            timeout_seconds=600,
        )
        exam_sections.append(batch_content)

    new_exam_analysis = "## 기출문제 상세 분석\n\n" + "\n\n".join(exam_sections)

    match = EXAM_SECTION_HEADING.search(summary)
    if match:
        next_match = NEXT_TOP_LEVEL_SECTION.search(summary, match.end())
        exam_end = next_match.start() if next_match else len(summary)
        summary = (
            summary[:match.start()].rstrip("\n")
            + "\n\n" + new_exam_analysis.strip() + "\n\n"
            + summary[exam_end:]
        )
    else:
        summary = summary.rstrip("\n") + "\n\n" + new_exam_analysis.strip() + "\n"

    summary = sanitize_generated_summary(summary, exam_figures)

    missing_figures = find_missing_exam_figures(summary, exam_figures)
    if missing_figures:
        print(f"  빠진 문항 {len(missing_figures)}개 보충 중...")
        for batch_start in range(0, len(missing_figures), EXAM_ANALYSIS_BATCH_SIZE):
            batch = missing_figures[batch_start:batch_start + EXAM_ANALYSIS_BATCH_SIZE]
            fill_request = build_exam_analysis_request(subject_name, lecture_title, batch)
            fill_content = generate_summary_with_codex(
                codex_path,
                fill_request,
                image_paths=_exam_batch_image_paths(batch, page_image_by_no),
                timeout_seconds=600,
            )
            match = EXAM_SECTION_HEADING.search(summary)
            next_match = NEXT_TOP_LEVEL_SECTION.search(summary, match.end()) if match else None
            insert_at = next_match.start() if next_match else len(summary)
            summary = (
                summary[:insert_at].rstrip("\n")
                + "\n\n" + fill_content.strip() + "\n\n"
                + summary[insert_at:]
            )
        summary = sanitize_generated_summary(summary, exam_figures)

    database.update_lecture_summary_content(lecture_id, summary, existing_summary["model"])
    print("DB 저장 완료.")

    try:
        try_sync_lecture_to_cloud(lecture_id)
        print("클라우드 동기화 완료.")
    except Exception as error:
        print(f"클라우드 동기화 실패(무시 가능): {error}")

    match = EXAM_SECTION_HEADING.search(summary)
    print("\n--- 새 기출 분석 미리보기 (앞부분) ---")
    print(summary[match.start():match.start() + 1200] if match else "(섹션을 찾지 못함)")


if __name__ == "__main__":
    main()
