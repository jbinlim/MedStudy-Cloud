"""임베디드 그림 후보가 페이지 대부분을 덮거나(수정 A) 본문 텍스트가
그림보다 더 큰 비중을 차지하는 슬라이드(수정 B)를 통째로 그림으로
잘못 삽입했던 문제를, 이미 생성된 강의에 대해 소급 수정한다.

지정한 페이지만 다시 탐지한다:
1. 새 필터/프롬프트가 적용된 임베디드 경로로 재시도
2. 그래도 채택된 그림이 없으면 벡터(전체 페이지 탐지) 경로로 재시도
3. 기존 lecture_figures에서 대상 페이지 행만 새 결과로 교체
4. 정리본 텍스트의 `[[FIGURE:옛키|...]]` 마커를 새 그림들의 마커로 교체
5. 클라우드 재동기화

사용법: python3 scripts/refix_lecture_figures.py <lecture_id> <page_no> [<page_no> ...]
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database
from codex_service import find_codex_cli
from cloud_sync import try_sync_lecture_to_cloud
from figure_service import (
    classify_figure_candidates_with_codex,
    crop_detected_figures,
    detect_figure_regions_with_codex,
    extract_embedded_figure_candidates,
)


def main():
    lecture_id = int(sys.argv[1])
    target_pages = {int(value) for value in sys.argv[2:]}
    if not target_pages:
        print("페이지 번호를 하나 이상 넘겨주세요.")
        return

    lecture = dict(database.get_lecture_by_id(lecture_id))
    pdf_path = lecture["pdf_path"]

    all_pages = [dict(row) for row in database.get_lecture_pages(lecture_id)]
    target_page_records = [p for p in all_pages if p["page_no"] in target_pages]
    if not target_page_records:
        print("해당 페이지를 강의록에서 찾지 못했습니다.")
        return

    codex_path = find_codex_cli()
    if not codex_path:
        print("Codex CLI를 찾지 못했습니다.")
        return

    figures_dir = Path(pdf_path).parent / "figures"
    print(f"[lecture {lecture_id}] 페이지 {sorted(target_pages)} 재탐지 시작")

    embedded_candidates = extract_embedded_figure_candidates(
        pdf_path, target_page_records, figures_dir
    )
    print(f"  임베디드 후보 {len(embedded_candidates)}개 추출")

    # automation_service.py의 배치 크기와 맞춰, 한 번에 너무 많은
    # 이미지를 첨부해 호출이 느려지거나 실패하지 않게 한다.
    EMBEDDED_CLASSIFY_BATCH_SIZE = 10
    VECTOR_DETECT_BATCH_SIZE = 8

    new_figures = []
    for start in range(0, len(embedded_candidates), EMBEDDED_CLASSIFY_BATCH_SIZE):
        batch = embedded_candidates[start:start + EMBEDDED_CLASSIFY_BATCH_SIZE]
        print(f"    임베디드 분류 배치 {start + len(batch)}/{len(embedded_candidates)}")
        new_figures.extend(
            classify_figure_candidates_with_codex(codex_path, batch)
        )
    print(f"  임베디드 경로에서 채택: {len(new_figures)}개")

    accepted_pages = {figure["page_no"] for figure in new_figures}
    still_missing = [
        page for page in target_page_records
        if page["page_no"] not in accepted_pages
    ]
    if still_missing:
        missing_nos = [page["page_no"] for page in still_missing]
        print(f"  {missing_nos}페이지는 전체 페이지 탐지로 재시도")
        for start in range(0, len(still_missing), VECTOR_DETECT_BATCH_SIZE):
            batch = still_missing[start:start + VECTOR_DETECT_BATCH_SIZE]
            print(f"    전체 페이지 탐지 배치 {start + len(batch)}/{len(still_missing)}")
            detections = detect_figure_regions_with_codex(codex_path, batch)
            new_figures.extend(
                crop_detected_figures(detections, batch, figures_dir)
            )

    if not new_figures:
        print("새로 채택된 그림이 없습니다. 정리본을 손대지 않고 종료합니다.")
        return

    for figure in new_figures:
        print(
            f"  새 그림: {figure['figure_key']} (페이지 {figure['page_no']}) "
            f"- {figure['caption']}"
        )

    # 기존 그림 목록에서 대상 페이지 행만 제거하고 새 결과로 교체한다.
    existing_figures = [dict(row) for row in database.get_lecture_figures(lecture_id)]
    old_keys_by_page = {}
    for figure in existing_figures:
        if figure["page_no"] in target_pages:
            old_keys_by_page.setdefault(figure["page_no"], []).append(
                figure["figure_key"]
            )
    kept_figures = [f for f in existing_figures if f["page_no"] not in target_pages]
    merged_figures = kept_figures + new_figures
    database.replace_lecture_figures(lecture_id, merged_figures)
    print("lecture_figures 갱신 완료.")

    # 정리본 텍스트의 마커 교체: 대상 페이지의 옛 figure_key를 참조하던
    # `[[FIGURE:...]]` 한 줄을, 그 페이지의 새 그림들 마커로 바꾼다. 옛
    # 키가 없어져도 렌더러는 조용히 빈 안내 박스만 보여줄 뿐 에러를
    # 내지 않아(summary_renderer.py) 여기서 안 고치면 눈에 안 띄게
    # 그림이 사라진다.
    summary = database.get_lecture_summary(lecture_id)
    if summary:
        content = summary["content"]
        for page_no, old_keys in old_keys_by_page.items():
            new_for_page = [f for f in new_figures if f["page_no"] == page_no]
            new_markers = "\n\n".join(
                f'[[FIGURE:{f["figure_key"]}|{f["caption"]}]]'
                for f in new_for_page
            )
            replaced_any = False
            for old_key in old_keys:
                pattern = re.compile(
                    r"\[\[FIGURE:" + re.escape(old_key) + r"\|[^\]]*\]\]"
                )
                if pattern.search(content):
                    if not replaced_any:
                        content = pattern.sub(new_markers, content, count=1)
                        replaced_any = True
                    else:
                        content = pattern.sub("", content)
            if not replaced_any and new_markers:
                print(
                    f"  경고: {page_no}페이지의 옛 마커를 정리본에서 못 찾았습니다"
                    " (수동으로 확인 필요)."
                )
        database.update_lecture_summary_content(
            lecture_id, content, summary["model"]
        )
        print("정리본 텍스트 마커 교체 완료.")
    else:
        print("이 강의에는 기존 정리본이 없어 텍스트 마커 교체는 건너뜁니다.")

    try:
        try_sync_lecture_to_cloud(lecture_id)
        print("클라우드 동기화 완료.")
    except Exception as error:
        print(f"클라우드 동기화 실패(무시 가능): {error}")


if __name__ == "__main__":
    main()
