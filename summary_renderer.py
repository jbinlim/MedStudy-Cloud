import re
from pathlib import Path

from PIL import Image


IMAGE_MARKER = re.compile(
    r"\[\[(PAGE_IMAGE|FIGURE|EXAM_FIGURE):([^|\]]+)\|([^\]]+)\]\]"
)
REPEATED_LABEL_ITEM = re.compile(
    r"^\s*[-*+]\s+(⭐|💬)\s*\*\*"
    r"(교수\s*강조|전사문\s*보충)\s*:\*\*\s*(.+?)\s*$"
)


def remove_accidental_strikethrough(markdown_text):
    """AI가 정정 표시로 만든 취소선 문법은 본문 텍스트로 표시한다."""
    cleaned = re.sub(
        r"(?<=\d)\s*~\s*(?=\d)",
        "–",
        markdown_text,
    )
    cleaned = re.sub(
        r"~~(.*?)~~",
        r"\1",
        cleaned,
        flags=re.DOTALL,
    )
    cleaned = cleaned.replace("~~", "")
    cleaned = re.sub(
        r"</?(?:del|s)\b[^>]*>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def group_repeated_summary_labels(markdown_text):
    """반복되는 강조 라벨을 소제목 하나와 간결한 목록으로 묶는다."""
    lines = markdown_text.splitlines()
    output = []
    index = 0

    while index < len(lines):
        first_match = REPEATED_LABEL_ITEM.match(lines[index])

        if not first_match:
            output.append(lines[index])
            index += 1
            continue

        items = []
        scan_index = index

        while scan_index < len(lines):
            match = REPEATED_LABEL_ITEM.match(lines[scan_index])
            if match:
                items.append(
                    (match.group(1), match.group(2), match.group(3))
                )
                scan_index += 1
                continue

            if (
                not lines[scan_index].strip()
                and scan_index + 1 < len(lines)
                and REPEATED_LABEL_ITEM.match(lines[scan_index + 1])
            ):
                scan_index += 1
                continue

            break

        if len(items) < 2:
            output.append(lines[index])
            index += 1
            continue

        previous_label = None
        for emoji, label, content in items:
            normalized_label = label.replace(" ", "")
            if normalized_label != previous_label:
                if output and output[-1] != "":
                    output.append("")
                display_label = (
                    "교수 강조"
                    if normalized_label == "교수강조"
                    else "강의록에 없던 전사문 보충"
                )
                output.append(f"#### {emoji} {display_label}")
                output.append("")
                previous_label = normalized_label

            output.append(f"- {content}")

        output.append("")
        index = scan_index

    return "\n".join(output)


def get_summary_image_width(image_path):
    """그림 비율별 표준 너비로 웹 화면의 크기 편차를 줄인다."""
    try:
        with Image.open(image_path) as image:
            source_width, source_height = image.size
    except Exception:
        return 420

    aspect_ratio = source_width / max(source_height, 1)

    if aspect_ratio < 0.72:
        preferred_width = 230
    elif aspect_ratio < 1.15:
        preferred_width = 360
    elif aspect_ratio < 1.75:
        preferred_width = 440
    else:
        preferred_width = 500

    return preferred_width


def render_summary_with_images(
    summary,
    lecture_pages,
    lecture_figures=None,
    exam_figures=None,
):
    # 실제 Streamlit 화면에서 그릴 때만 가져온다. generation_worker.py 같은
    # 백그라운드 워커는 이 파일의 순수 텍스트 헬퍼(IMAGE_MARKER 등)만
    # 쓰는데, 모듈 최상단에서 `import streamlit`을 하면 워커 프로세스에도
    # Streamlit의 네트워킹/웹소켓 초기화가 함께 로드된다. 이 상태에서
    # 이후 Codex CLI를 subprocess로 실행하며 fork()하면 macOS의
    # Network.framework atfork 핸들러가 자식 프로세스를 크래시시키는
    # 경우가 있어(EXC_BAD_ACCESS in NEFlowDirectorDestroy), 실제로
    # Streamlit 렌더링이 필요한 이 함수 안에서만 지연 임포트한다.
    import streamlit as st

    summary = remove_accidental_strikethrough(summary)
    summary = group_repeated_summary_labels(summary)
    pages_by_number = {
        page["page_no"]: page
        for page in lecture_pages
    }
    figures_by_key = {
        figure["figure_key"]: figure
        for figure in (lecture_figures or [])
    }
    exam_figures_by_key = {
        figure["figure_key"]: figure
        for figure in (exam_figures or [])
    }
    cursor = 0

    for match in IMAGE_MARKER.finditer(summary):
        preceding_markdown = summary[cursor:match.start()]

        if preceding_markdown.strip():
            st.markdown(preceding_markdown)

        marker_type = match.group(1)
        marker_id = match.group(2).strip()
        caption = match.group(3).strip()

        if marker_type == "EXAM_FIGURE":
            figure = exam_figures_by_key.get(marker_id)
            image_path = figure["image_path"] if figure else None
            source_label = figure["source_label"] if figure else marker_id
        elif marker_type == "FIGURE":
            figure = figures_by_key.get(marker_id)
            image_path = (
                figure["image_path"]
                if figure
                else None
            )
            source_label = (
                f'PDF {figure["page_no"]}페이지'
                if figure
                else marker_id
            )
        else:
            page_no = int(marker_id)
            page = pages_by_number.get(page_no)
            image_path = (
                page["image_path"]
                if page
                else None
            )
            source_label = f"PDF {page_no}페이지"

        if image_path and Path(image_path).exists():
            st.image(
                image_path,
                caption=(
                    f"{source_label} - {caption}"
                ),
                width=get_summary_image_width(image_path),
            )
        else:
            st.info(
                f"{source_label} 이미지: {caption}"
            )

        cursor = match.end()

    remaining_markdown = summary[cursor:]

    if remaining_markdown.strip():
        st.markdown(remaining_markdown)
