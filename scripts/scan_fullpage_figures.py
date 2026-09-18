"""이미 생성된 강의들 중, 저장된 그림 파일이 사실상 페이지 전체를
그대로 캡처한 것에 가까운 경우를 찾아낸다(수정 A/B 적용 전에 생성된
강의들의 소급 수정 대상을 고르기 위한 진단 스크립트).

DB의 `x0..x1` 좌표는 임베디드 경로로 채택된 그림이면 항상
`0,0,1000,1000` 고정값이라 그 자체로는 진단에 못 쓴다. 대신 저장된
JPEG의 실제 픽셀 크기를, 그 페이지의 PDF 페이지 크기(2배 렌더 배율
기준)와 비교해 면적 비율을 계산한다.

사용법: python3 scripts/scan_fullpage_figures.py [면적비율 임계값(기본 0.90)]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf
from PIL import Image

import database


def main():
    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else 0.90

    flagged = []
    for subject in database.get_subjects():
        for lecture in database.get_lectures(subject["id"]):
            lecture = dict(lecture)
            pdf_path = lecture.get("pdf_path")
            if not pdf_path or not Path(pdf_path).exists():
                continue

            # figure_key 형식은 p{page:03d}_e{n:02d}(임베디드 경로) 또는
            # p{page:03d}_f{n:02d}(벡터 탐지 경로)다. 벡터 경로는 이미
            # crop_detected_figures에서 area_ratio>0.88을 걸러내므로
            # 진단 대상에서 제외하고, 안전장치가 전혀 없던 임베디드
            # 경로(`_e`)만 검사한다.
            figures = [dict(row) for row in database.get_lecture_figures(lecture["id"])]
            embedded_figures = [
                figure for figure in figures
                if _is_embedded_figure_key(figure["figure_key"])
            ]
            if not embedded_figures:
                continue

            try:
                doc = pymupdf.open(pdf_path)
            except Exception:
                continue

            for figure in embedded_figures:
                page_no = figure["page_no"]
                if page_no < 1 or page_no > len(doc):
                    continue
                page = doc[page_no - 1]
                page_area_2x = (page.rect.width * 2) * (page.rect.height * 2)
                if page_area_2x <= 0:
                    continue

                image_path = Path(figure["image_path"])
                if not image_path.exists():
                    continue
                try:
                    with Image.open(image_path) as image:
                        width, height = image.size
                except Exception:
                    continue

                ratio = (width * height) / page_area_2x
                if ratio > threshold:
                    flagged.append(
                        (
                            lecture["id"],
                            subject["name"],
                            lecture["lecture_no"],
                            lecture["title"],
                            page_no,
                            figure["figure_key"],
                            round(ratio, 3),
                        )
                    )
            doc.close()

    if not flagged:
        print(f"임계값 {threshold} 기준으로 걸린 항목이 없습니다.")
        return

    print(f"임계값 {threshold} 기준 {len(flagged)}건 발견:\n")
    by_lecture = {}
    for lecture_id, subject_name, lecture_no, title, page_no, figure_key, ratio in flagged:
        by_lecture.setdefault((lecture_id, subject_name, lecture_no, title), []).append(
            (page_no, figure_key, ratio)
        )

    for (lecture_id, subject_name, lecture_no, title), items in by_lecture.items():
        pages = sorted({page_no for page_no, _, _ in items})
        print(f"lecture_id={lecture_id} {subject_name} {lecture_no}강 {title}")
        for page_no, figure_key, ratio in sorted(items):
            print(f"    {figure_key} (페이지 {page_no}) 면적비율={ratio}")
        print(
            f"  -> python3 scripts/refix_lecture_figures.py {lecture_id} "
            + " ".join(str(p) for p in pages)
        )
        print()


def _is_embedded_figure_key(figure_key):
    """`p003_e01`처럼 임베디드 경로로 만들어진 키인지 확인한다."""
    parts = figure_key.rsplit("_", 1)
    return len(parts) == 2 and parts[1].startswith("e")


if __name__ == "__main__":
    main()
