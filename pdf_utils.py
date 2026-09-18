from pathlib import Path

import pymupdf


def _has_usable_embedded_text(text, minimum_chars):
    compact = "".join(text.split())
    if len(compact) < minimum_chars:
        return False
    readable = sum(
        character.isalnum() or "가" <= character <= "힣"
        for character in compact
    )
    return readable / len(compact) >= 0.6


def extract_pdf_page_texts(pdf_path):
    """PDF의 텍스트를 페이지 순서대로 추출한다."""
    page_texts = []

    with pymupdf.open(Path(pdf_path)) as document:
        for page in document:
            page_texts.append(
                page.get_text("text").strip()
            )

    return page_texts


def process_pdf_pages(
    pdf_path,
    pages_dir,
    tessdata_dir,
    dpi=150,
    progress_callback=None,
    embedded_text_ocr_threshold=80,
):
    """페이지 이미지를 만들고 필요한 페이지만 한국어+영어 OCR 처리한다."""
    pdf_path = Path(pdf_path)
    pages_dir = Path(pages_dir)
    tessdata_dir = Path(tessdata_dir)
    pages_dir.mkdir(parents=True, exist_ok=True)

    processed_pages = []

    with pymupdf.open(pdf_path) as document:
        total_pages = len(document)
        scale = dpi / 72
        matrix = pymupdf.Matrix(scale, scale)

        for page_index, page in enumerate(document):
            page_no = page_index + 1
            embedded_text = page.get_text("text").strip()
            image_path = (
                pages_dir
                / f"page_{page_no:03d}.jpg"
            )

            pixmap = page.get_pixmap(
                matrix=matrix,
                alpha=False,
            )
            pixmap.pil_save(
                image_path,
                format="JPEG",
                quality=88,
            )

            # PDF에 선택 가능한 텍스트가 충분하면 동일한 내용을 OCR로
            # 다시 읽지 않는다. 스캔본/이미지 슬라이드만 OCR을 수행한다.
            if _has_usable_embedded_text(
                embedded_text,
                embedded_text_ocr_threshold,
            ):
                ocr_text = ""
            else:
                textpage = page.get_textpage_ocr(
                    language="kor+eng",
                    dpi=dpi,
                    full=True,
                    tessdata=str(tessdata_dir),
                )
                ocr_text = page.get_text(
                    "text",
                    textpage=textpage,
                ).strip()

            representative_text = (
                embedded_text
                if embedded_text
                else ocr_text
            )

            processed_pages.append(
                {
                    "text": representative_text,
                    "embedded_text": embedded_text,
                    "ocr_text": ocr_text,
                    "image_path": str(image_path),
                }
            )

            if progress_callback:
                progress_callback(
                    page_no,
                    total_pages,
                )

    return processed_pages
