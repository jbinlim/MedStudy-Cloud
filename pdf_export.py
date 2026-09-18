import base64
import html
import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pymupdf
from PIL import Image

from summary_renderer import (
    IMAGE_MARKER,
    group_repeated_summary_labels,
    remove_accidental_strikethrough,
)


FONT_DIR = Path("/System/Library/Fonts")
PDF_EXPORT_VERSION = "2026-09-13-v41"

PDF_SYMBOL_REPLACEMENTS = {
    "⭐": "[교수 강조]",
    "💬": "[전사문 보충]",
    "🎯": "[기출]",
    "🧠": "[암기]",
    "⚠️": "[주의]",
    "⚠": "[주의]",
}


def _normalize_pdf_symbols(text):
    """색상 이모지를 PDF에서 깨지지 않는 짧은 글자 표지로 바꾼다."""
    for symbol, replacement in PDF_SYMBOL_REPLACEMENTS.items():
        text = text.replace(symbol, replacement)
    return text


def _format_figure_caption(source, caption):
    """그림 설명에 반복된 PDF 출처를 제거해 출처를 한 번만 표시한다."""
    source = (source or "").strip()
    caption = (caption or "").strip()

    if source:
        escaped_source = re.escape(source)
        caption = re.sub(
            rf"^\s*{escaped_source}\s*[.·,:：\-–—]?\s*",
            "",
            caption,
            flags=re.IGNORECASE,
        )
        caption = re.sub(
            rf"\s*[\(（\[]\s*{escaped_source}(?:\s*참고)?\s*[\)）\]]\s*[.]?\s*$",
            "",
            caption,
            flags=re.IGNORECASE,
        ).strip()

    if source and caption:
        return f"{source} - {caption}"
    return source or caption


LATEX_COMMANDS = {
    r"\pOH": "pOH",
    r"\pH": "pH",
    r"\rightleftharpoons": "⇌",
    r"\leftrightharpoons": "⇋",
    r"\rightarrow": "→",
    r"\leftarrow": "←",
    r"\times": "×",
    r"\approx": "≈",
    r"\leq": "≤",
    r"\geq": "≥",
    r"\neq": "≠",
    r"\pm": "±",
    r"\cdot": "·",
    r"\Delta": "Δ",
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\log": "log",
    r"\ln": "ln",
}


def _balanced_brace_content(value, opening_index):
    """중첩 중괄호를 포함한 LaTeX 인수와 닫는 위치를 반환한다."""
    if opening_index >= len(value) or value[opening_index] != "{":
        return None, opening_index
    depth = 0
    for index in range(opening_index, len(value)):
        if value[index] == "{":
            depth += 1
        elif value[index] == "}":
            depth -= 1
            if depth == 0:
                return value[opening_index + 1:index], index + 1
    return None, opening_index


def _replace_latex_fractions(value):
    """지수처럼 중첩된 중괄호가 있는 frac도 안전하게 변환한다."""
    cursor = 0
    output = []
    while True:
        command_index = value.find(r"\frac", cursor)
        if command_index < 0:
            output.append(value[cursor:])
            break
        output.append(value[cursor:command_index])
        numerator, after_numerator = _balanced_brace_content(
            value, command_index + len(r"\frac")
        )
        denominator_start = after_numerator
        while denominator_start < len(value) and value[denominator_start].isspace():
            denominator_start += 1
        denominator, after_denominator = _balanced_brace_content(
            value, denominator_start
        )
        if numerator is None or denominator is None:
            # 불완전한 명령은 '\\frac' 글자가 PDF에 그대로 남지 않게 한다.
            output.append("/")
            cursor = command_index + len(r"\frac")
            continue
        output.append(
            '<span class="math-frac">('
            + _replace_latex_fractions(numerator)
            + ")/("
            + _replace_latex_fractions(denominator)
            + ")</span>"
        )
        cursor = after_denominator
    return "".join(output)


def _latex_to_html(expression, display=False):
    """PDF 엔진이 지원하지 않는 LaTeX를 읽기 쉬운 HTML 수식으로 바꾼다."""
    value = html.escape(expression.strip())
    # 긴 명령(예: \rightleftharpoons)을 먼저 바꾼 뒤 크기 조절용
    # \left, \right 표식만 제거해야 명령 일부가 잘리지 않는다.
    for command, replacement in LATEX_COMMANDS.items():
        value = value.replace(command, replacement)
    value = value.replace(r"\left", "").replace(r"\right", "")
    value = re.sub(r"\\(?:mathrm|text)\{([^{}]*)\}", r"\1", value)

    value = _replace_latex_fractions(value)
    value = value.replace(r"\boxed", "")

    value = re.sub(r"\^\{([^{}]+)\}", r"<sup>\1</sup>", value)
    value = re.sub(r"_\{([^{}]+)\}", r"<sub>\1</sub>", value)
    # H^++A^-에서 '+A'까지 하나의 지수로 먹지 않도록 부호 하나 또는
    # 부호가 붙은 숫자만 unbraced 지수로 인정한다.
    value = re.sub(r"\^([+\-]?\d+|[+\-])", r"<sup>\1</sup>", value)
    value = re.sub(r"_([0-9A-Za-z]+)", r"<sub>\1</sub>", value)

    # LaTeX의 간격 명령은 수식 의미가 아니라 화면 배치용이다. PDF 엔진에
    # 그대로 넘기면 `\quad` 같은 글자가 노출되므로 모두 일반 공백으로 바꾼다.
    value = re.sub(
        r"\\(?:qquad|quad|enspace|hspace\{[^{}]*\})",
        " ",
        value,
    )
    value = value.replace(r"\;", " ").replace(r"\:", " ")
    value = value.replace(r"\,", " ").replace(r"\!", "")
    value = value.replace(r"\ ", " ")
    value = value.replace("{", "").replace("}", "")
    css_class = "math-display" if display else "math-inline"
    return f'<span class="{css_class}">{value}</span>'


def _inline_markdown(text):
    math_fragments = []

    def preserve_math(match, display=False):
        placeholder = f"@@MEDSTUDYMATH{len(math_fragments)}@@"
        math_fragments.append(_latex_to_html(match.group(1), display=display))
        return placeholder

    text = re.sub(
        r"\\\[(.*?)\\\]",
        lambda match: preserve_math(match, display=True),
        text.strip(),
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\\\((.*?)\\\)",
        lambda match: preserve_math(match, display=False),
        text,
        flags=re.DOTALL,
    )

    # Apple SD Gothic Neo에는 일부 유니코드 위첨자(특히 ⁹ 등)가 없어
    # PDF에서 네모로 보일 수 있다. 일반 숫자/기호를 실제 HTML 위첨자와
    # 아래첨자로 배치하면 한글·영문과 같은 글꼴로 안정적으로 출력된다.
    superscript_map = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁽⁾", "0123456789+-()")
    subscript_map = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋₍₎", "0123456789+-()")

    def preserve_script(match, tag, translation):
        placeholder = f"@@MEDSTUDYMATH{len(math_fragments)}@@"
        math_fragments.append(
            f"<{tag}>{match.group(0).translate(translation)}</{tag}>"
        )
        return placeholder

    text = re.sub(
        r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁽⁾]+",
        lambda match: preserve_script(match, "sup", superscript_map),
        text,
    )
    text = re.sub(
        r"[₀₁₂₃₄₅₆₇₈₉₊₋₍₎]+",
        lambda match: preserve_script(match, "sub", subscript_map),
        text,
    )
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(
        r"\[\[EXAM_HIGHLIGHT:(.+?)\]\]",
        r'<span class="past-exam-highlight">\1</span>',
        text,
    )
    for index, fragment in enumerate(math_fragments):
        text = text.replace(f"@@MEDSTUDYMATH{index}@@", fragment)
    return text


def _expand_structured_exam_highlights(text):
    """표의 여러 셀이나 여러 줄을 감싼 기출 표식을 셀·줄별 표식으로 나눈다."""

    def expand(match):
        content = match.group(1)

        if "|" in content:
            # 표의 한 행 전체가 통째로 감싸인 경우, 앞뒤 `|` 를 그대로
            # 남겨 둬야 아래 표 변환 로직이 여전히 "표의 한 행"으로
            # 인식한다. 예전에는 빈 칸(선행/후행 `|` 의 흔적)을 걸러내며
            # 양 끝 `|` 까지 사라져, 강조 표시가 붙은 행만 표에서
            # 튕겨나가 표가 두 조각으로 쪼개지고 그 다음 남은 첫 행이
            # 엉뚱하게 머리글로 쓰이는 문제가 있었다.
            cells = content.split("|")
            return "|".join(
                f"[[EXAM_HIGHLIGHT:{cell.strip()}]]" if cell.strip() else cell
                for cell in cells
            )

        if "\n" in content:
            expanded_lines = []
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if not line:
                    continue

                label_match = re.match(r"^([^:]{1,40}:)\s*(.+)$", line)
                if label_match:
                    expanded_lines.append(
                        f"{label_match.group(1)} "
                        f"[[EXAM_HIGHLIGHT:{label_match.group(2)}]]"
                    )
                else:
                    expanded_lines.append(
                        f"[[EXAM_HIGHLIGHT:{line}]]"
                    )
            return "\n".join(expanded_lines)

        return match.group(0)

    return re.sub(
        r"\[\[EXAM_HIGHLIGHT:(.*?)\]\]",
        expand,
        text,
        flags=re.DOTALL,
    )


def _trim_image_content(image, padding=12):
    """크롭 파일에 남은 넓은 흰 여백을 제거해 실제 그림 크기를 기준으로 삼는다."""
    rgb_image = image.convert("RGB")
    grayscale = rgb_image.convert("L")
    content_mask = grayscale.point(
        lambda pixel: 255 if pixel < 245 else 0
    )
    content_box = content_mask.getbbox()

    if not content_box:
        return rgb_image

    left, top, right, bottom = content_box
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(rgb_image.width, right + padding)
    bottom = min(rgb_image.height, bottom + padding)

    content_width = right - left
    content_height = bottom - top
    original_area = max(rgb_image.width * rgb_image.height, 1)
    content_area = content_width * content_height

    if content_area < original_area * 0.03:
        return rgb_image

    return rgb_image.crop((left, top, right, bottom))


def _trim_exam_image_content(image, padding=12):
    """문항 위쪽과 멀리 떨어진 작은 답안 메모가 큰 여백을 만들면 제외한다."""
    rgb_image = image.convert("RGB")
    grayscale = rgb_image.convert("L")
    mask = grayscale.point(lambda pixel: 255 if pixel < 245 else 0)
    width, height = mask.size
    threshold = max(2, width // 1000)
    active_rows = []
    row_ink = {}
    for y in range(height):
        ink = mask.crop((0, y, width, y + 1)).histogram()[255]
        if ink >= threshold:
            active_rows.append(y)
            row_ink[y] = ink

    if not active_rows:
        return rgb_image

    bands = []
    for y in active_rows:
        if not bands or y > bands[-1][1] + 3:
            bands.append([y, y])
        else:
            bands[-1][1] = y

    if len(bands) >= 2:
        # 본문 아래 멀리 떨어진 작은 답안 메모나 시험지 쪽번호를 제거한다.
        # 위쪽 전체 문항의 잉크량이 아래 조각보다 충분히 클 때만 잘라서
        # 실제 그림·표가 아래에 놓인 문항은 보존한다.
        cutoff = None
        for band_index in range(len(bands) - 1):
            upper_end = bands[band_index][1]
            lower_start = bands[band_index + 1][0]
            upper_ink = sum(row_ink.get(y, 0) for y in range(bands[0][0], upper_end + 1))
            lower_ink = sum(row_ink.get(y, 0) for y in range(lower_start, height))
            if (
                lower_start - upper_end > max(35, round(height * 0.12))
                and upper_ink >= max(1, lower_ink) * 4
            ):
                cutoff = upper_end
                break
        if cutoff is not None:
            first_start = bands[0][0]
            upper_mask = mask.crop((0, first_start, width, cutoff + 1))
            upper_box = upper_mask.getbbox()
            if upper_box:
                left, top, right, bottom = upper_box
                return rgb_image.crop((
                    max(0, left - padding),
                    max(0, first_start + top - padding),
                    min(width, right + padding),
                    min(height, first_start + bottom + padding),
                ))

    return _trim_image_content(rgb_image, padding=padding)


@lru_cache(maxsize=512)
def _image_data_uri(image_path, exam_image=False):
    path = Path(image_path)
    output = BytesIO()

    with Image.open(path) as image:
        image = (
            _trim_exam_image_content(image)
            if exam_image else _trim_image_content(image)
        )
        image.thumbnail((900, 700), Image.Resampling.LANCZOS)
        image.save(output, format="JPEG", quality=72, optimize=True)

    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


@lru_cache(maxsize=512)
def _image_display_size(image_path, exam_image=False):
    """그림 비율별 표준 상자에 맞춰 PDF 표시 크기를 일정하게 만든다."""
    with Image.open(image_path) as image:
        trimmed_image = (
            _trim_exam_image_content(image)
            if exam_image else _trim_image_content(image)
        )
        width, height = trimmed_image.size

    aspect_ratio = width / max(height, 1)

    if exam_image:
        target_width = 470
        target_height = target_width / aspect_ratio
        if target_height > 430:
            scale = 430 / target_height
            target_width *= scale
            target_height = 430
        return max(1, round(target_width)), max(1, round(target_height))

    if aspect_ratio < 0.72:
        target_height = 270
        target_width = target_height * aspect_ratio
    elif aspect_ratio < 1.15:
        target_width = 270
        target_height = target_width / aspect_ratio
    elif aspect_ratio < 1.75:
        target_width = 330
        target_height = target_width / aspect_ratio
    else:
        target_width = 360
        target_height = target_width / aspect_ratio

    if target_height > 280:
        scale = 280 / target_height
        target_width *= scale
        target_height = 280

    layout_scale = 0.88
    return (
        max(1, round(target_width * layout_scale)),
        max(1, round(target_height * layout_scale)),
    )


def _resolve_image(
    marker_type, marker_id, lecture_pages, lecture_figures, exam_figures=None
):
    if marker_type == "EXAM_FIGURE":
        figure = next(
            (
                item for item in (exam_figures or [])
                if item["figure_key"] == marker_id
                and item["source_kind"] == "lecture"
            ),
            None,
        )
        if figure:
            return figure["image_path"], figure["source_label"]
    elif marker_type == "FIGURE":
        figure = next(
            (
                item
                for item in lecture_figures
                if item["figure_key"] == marker_id
            ),
            None,
        )
        if figure:
            return figure["image_path"], f'PDF {figure["page_no"]}페이지'
    else:
        try:
            page_no = int(marker_id)
        except ValueError:
            return None, marker_id
        page = next(
            (item for item in lecture_pages if item["page_no"] == page_no),
            None,
        )
        if page:
            return page["image_path"], f"PDF {page_no}페이지"

    return None, marker_id


def _summary_to_html(
    summary,
    lecture_pages,
    lecture_figures,
    exam_figures=None,
    page_break_before_images=None,
    page_break_before_headings=None,
    page_break_before_questions=None,
):
    summary = remove_accidental_strikethrough(summary)
    summary = _expand_structured_exam_highlights(summary)
    summary = group_repeated_summary_labels(summary)
    summary = _normalize_pdf_symbols(summary)
    lines = summary.splitlines()
    output = []
    paragraph = []
    in_list = False
    index = 0
    image_index = 0
    heading_index = 0
    question_index = 0
    in_memory_section = False
    page_break_before_images = page_break_before_images or set()
    page_break_before_headings = page_break_before_headings or set()
    page_break_before_questions = page_break_before_questions or set()

    def close_paragraph():
        nonlocal paragraph
        if paragraph:
            output.append(
                f"<p>{_inline_markdown(' '.join(paragraph))}</p>"
            )
            paragraph = []

    def close_list():
        nonlocal in_list
        if in_list:
            output.append("</ul>")
            in_list = False

    while index < len(lines):
        stripped = lines[index].strip()

        if not stripped:
            close_paragraph()
            close_list()
            index += 1
            continue

        marker = IMAGE_MARKER.fullmatch(stripped)
        if marker:
            close_paragraph()
            close_list()
            image_path, source = _resolve_image(
                marker.group(1),
                marker.group(2).strip(),
                lecture_pages,
                lecture_figures,
                exam_figures,
            )
            caption = marker.group(3).strip()
            if image_path and Path(image_path).exists():
                image_index += 1
                display_width, display_height = _image_display_size(
                    image_path,
                    exam_image=(marker.group(1) == "EXAM_FIGURE"),
                )
                break_style = (
                    "page-break-before:always;"
                    if image_index in page_break_before_images
                    else ""
                )
                output.append(
                    f'<table class="figure-table" style="{break_style}">'
                    '<tr><td>'
                    f'<img src="{_image_data_uri(image_path, exam_image=(marker.group(1) == "EXAM_FIGURE"))}" '
                    f'width="{display_width}" height="{display_height}" '
                    f'style="min-width:{display_width}px; '
                    f'min-height:{display_height}px" />'
                    '<p class="figure-caption">'
                    f'{html.escape(_format_figure_caption(source, caption))}</p>'
                    '</td></tr></table>'
                )
            index += 1
            continue

        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            close_paragraph()
            close_list()
            heading_index += 1
            level = len(heading.group(1))
            heading_text = heading.group(2)
            if level <= 2:
                in_memory_section = heading_text.startswith("5.") and "암기" in heading_text

            # 과거 정리본은 암기 주제가 `#### 제목`, 최근 정리본은
            # `### [암기] 암기: 제목`으로 저장되어 있다. 두 형식과
            # 그 아래의 중첩 소제목·목록을 하나의 카드로 묶는다.
            if in_memory_section and level >= 3:
                probe = index + 1
                card_lines = []
                while probe < len(lines):
                    raw_candidate = lines[probe]
                    candidate = raw_candidate.strip()
                    if not candidate:
                        probe += 1
                        continue
                    next_heading = re.match(r"^(#{1,4})\s+(.+)$", candidate)
                    if next_heading and len(next_heading.group(1)) <= level:
                        break
                    if next_heading:
                        card_lines.append(("subtitle", 0, next_heading.group(2)))
                        probe += 1
                        continue
                    bullet_match = re.match(r"^[-*+]\s+(.+)$", candidate)
                    if bullet_match:
                        indentation = len(raw_candidate) - len(raw_candidate.lstrip())
                        card_lines.append(("bullet", min(2, indentation // 2), bullet_match.group(1)))
                    else:
                        card_lines.append(("note", 0, candidate))
                    probe += 1
                if card_lines:
                    is_warning = "주의" in heading_text or "[주의]" in heading_text
                    card_kind = "warning" if is_warning else "memory"
                    clean_title = re.sub(r"^\[(?:암기|주의)\]\s*", "", heading_text)
                    clean_title = re.sub(r"^(?:암기|주의)\s*[:：—–-]\s*", "", clean_title)
                    label = "[주의]" if is_warning else "[암기]"

                    def _render_study_line(line_kind, depth, content):
                        if line_kind == "subtitle":
                            return (
                                '<p class="study-subtitle">'
                                f'{_inline_markdown(content)}</p>'
                            )
                        if line_kind == "bullet":
                            return (
                                f'<p class="study-detail depth-{depth}">'
                                f'•&nbsp; {_inline_markdown(content)}</p>'
                            )
                        return (
                            '<p class="study-note">'
                            f'{_inline_markdown(content)}</p>'
                        )

                    # 세부 항목이 많다고 표를 여러 개로 나누면, 페이지 안에서도
                    # 항목 사이 간격이 벌어져 한 주제가 서로 다른 문단처럼
                    # 보인다. 페이지 나눔은 뒤쪽의 반복 배치 최적화가
                    # 담당하므로, 여기서는 항상 표 하나로 묶어 한 덩어리로
                    # 보이게 한다.
                    output.append(
                        f'<table class="study-card {card_kind}"><tbody><tr><td>'
                    )
                    output.append(
                        '<p class="study-title">'
                        f'{label} {_inline_markdown(clean_title)}'
                        '</p>'
                    )
                    output.extend(
                        _render_study_line(*line) for line in card_lines
                    )
                    output.append('</td></tr></tbody></table>')
                    index = probe
                    continue
            break_marker = (
                '<div style="page-break-before:always"></div>'
                if heading_index in page_break_before_headings
                else ""
            )
            # 기출 소제목 바로 아래의 '문제의 요점' 인용문은 작은 표로
            # 묶는다. 페이지 끝에서 제목만 남는 것은 막되, 별도의 강제
            # 페이지 넘김이 연쇄적으로 추가되어 큰 여백이 생기지 않는다.
            next_index = index + 1
            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1
            next_line = (
                lines[next_index].strip()
                if next_index < len(lines)
                else ""
            )
            next_image_marker = IMAGE_MARKER.fullmatch(next_line)
            if level >= 3 and next_image_marker:
                image_path, source = _resolve_image(
                    next_image_marker.group(1),
                    next_image_marker.group(2).strip(),
                    lecture_pages,
                    lecture_figures,
                    exam_figures,
                )
                if image_path and Path(image_path).exists():
                    image_index += 1
                    display_width, display_height = _image_display_size(
                        image_path,
                        exam_image=(next_image_marker.group(1) == "EXAM_FIGURE"),
                    )
                    image_break_style = (
                        "page-break-before:always;"
                        if image_index in page_break_before_images
                        else ""
                    )
                    caption = next_image_marker.group(3).strip()
                    output.append(
                        f'{break_marker}<table class="heading-figure" '
                        f'style="{image_break_style}"><tr><td>'
                        f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>"
                        f'<img src="{_image_data_uri(image_path, exam_image=(next_image_marker.group(1) == "EXAM_FIGURE"))}" '
                        f'width="{display_width}" height="{display_height}" '
                        f'style="min-width:{display_width}px; '
                        f'min-height:{display_height}px" />'
                        '<p class="figure-caption">'
                        f'{html.escape(_format_figure_caption(source, caption))}</p>'
                        "</td></tr></table>"
                    )
                    index = next_index + 1
                    continue
            if level >= 3 and next_line.startswith(">"):
                # 텍스트형 기출 카드는 제목·문제 요점만 같은 표에 묶어
                # "제목만" 남는 상황을 막는다. 세부 bullet(필요한 핵심
                # 개념, 정답 및 판단 근거 등)까지 같은 표에 묶으면 표
                # 전체가 하나의 분리 불가 블록이 되어, 페이지 끝에서 다
                # 들어가지 못할 때 제목·요점까지 통째로 다음 페이지로
                # 밀리며 앞 페이지에 큰 빈 공간을 남긴다. 세부 bullet은
                # 표 밖에서 독립적으로 흐르게 해 필요하면 그 사이에서만
                # 페이지가 넘어가게 한다.
                card_end = next_index + 1
                card_items = []
                if "🎯" in heading.group(2) or "[기출]" in heading.group(2):
                    probe = card_end
                    while probe < len(lines):
                        candidate = lines[probe].strip()
                        if not candidate:
                            probe += 1
                            continue
                        bullet_match = re.match(r"^[-*+]\s+(.+)$", candidate)
                        if not bullet_match:
                            break
                        card_items.append(bullet_match.group(1))
                        probe += 1
                    if card_items:
                        card_end = probe

                output.append(
                    f'{break_marker}<table class="heading-intro"><tr><td>'
                    f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>"
                    f"<blockquote>{_inline_markdown(next_line[1:])}</blockquote>"
                    "</td></tr></table>"
                )
                if card_items:
                    output.append('<ul class="exam-card-details">')
                    output.extend(
                        f"<li>{_inline_markdown(item)}</li>"
                        for item in card_items
                    )
                    output.append("</ul>")
                index = card_end
            else:
                output.append(
                    f"{break_marker}<h{level}>"
                    f"{_inline_markdown(heading.group(2))}</h{level}>"
                )
                index += 1
            continue

        if stripped.startswith(">"):
            close_paragraph()
            close_list()
            output.append(
                f"<blockquote>{_inline_markdown(stripped[1:])}</blockquote>"
            )
            index += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            close_paragraph()
            close_list()
            table_rows = []
            while index < len(lines):
                row = lines[index].strip()
                if not (row.startswith("|") and row.endswith("|")):
                    break
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                    table_rows.append(cells)
                index += 1
            if table_rows:
                headers = table_rows[0]
                output.append('<div class="comparison-list">')
                for cells in table_rows[1:]:
                    # `<div>`는 MuPDF Story가 페이지 중간에서 나눌 수 있다.
                    # 비교 카드 한 줄이 갈라지지 않도록 `<table>`로 감싼다.
                    output.append(
                        '<table class="comparison-card"><tbody><tr><td>'
                    )
                    for cell_no, cell in enumerate(cells):
                        header = (
                            headers[cell_no]
                            if cell_no < len(headers)
                            else f"항목 {cell_no + 1}"
                        )
                        if not cell.strip():
                            continue
                        if header.strip():
                            output.append(
                                '<p class="comparison-row">'
                                f'<strong class="comparison-label">'
                                f'{_inline_markdown(header)}:</strong> '
                                f'{_inline_markdown(cell)}</p>'
                            )
                        else:
                            # 표 머리글 칸이 비어 있으면(AI가 첫 열에
                            # 제목을 안 채운 경우) 빈 라벨에 콜론만 붙는
                            # "` : 값`" 형태를 만들지 않고 값만 그대로
                            # 보여준다.
                            output.append(
                                f'<p class="comparison-row">'
                                f'{_inline_markdown(cell)}</p>'
                            )
                    output.append("</td></tr></tbody></table>")
                output.append("</div>")
            continue

        numbered_item = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if numbered_item:
            close_paragraph()
            close_list()
            question_no = numbered_item.group(1)
            question = numbered_item.group(2).rstrip()
            answer = None
            next_index = index + 1

            while next_index < len(lines) and not lines[next_index].strip():
                next_index += 1

            if next_index < len(lines):
                answer_match = re.match(
                    r"^\*\*정답:\*\*\s*(.+)$",
                    lines[next_index].strip(),
                )
                if answer_match:
                    answer = answer_match.group(1)

            if answer:
                question_index += 1
                break_marker = (
                    '<div style="page-break-before:always"></div>'
                    if question_index in page_break_before_questions
                    else ""
                )
                # `<div>`는 MuPDF Story가 그대로 두지 않고 페이지 중간에서
                # 나눌 수 있다(위 암기 카드 관련 설명 참고). 질문과 정답이
                # 서로 다른 페이지로 갈라지지 않도록 `<table>`로 감싼다.
                output.append(f'{break_marker}<table class="qa-card"><tbody><tr><td>')
                output.append(
                    '<p class="qa-question">'
                    f'<strong>Q{question_no}.</strong> '
                    f'{_inline_markdown(question)}</p>'
                )
                output.append(
                    '<p class="qa-answer"><strong>정답</strong><br>'
                    f'{_inline_markdown(answer)}</p>'
                )
                index = next_index + 1
                output.append("</td></tr></tbody></table>")
            else:
                output.append(
                    '<p class="numbered-item">'
                    f'<strong>{question_no}.</strong> '
                    f'{_inline_markdown(question)}</p>'
                )
                index += 1
            continue

        study_item = re.match(
            # 이모지를 PDF용 표지로 치환하면 `[암기]`가 굵은 글씨
            # 안쪽으로 들어오는 경우가 있다. 두 형태 모두 카드로
            # 인식해야 하위 목록이 페이지 중간에서 분리되지 않는다.
            r"^[-*+]\s+((?:\*\*)?\[(?:암기|주의)\].+?\*\*?)$",
            stripped,
        )
        if study_item:
            close_paragraph()
            close_list()
            raw_title = study_item.group(1).lstrip("*")
            card_kind = (
                "memory" if raw_title.startswith("[암기]")
                else "warning"
            )
            title_text = re.sub(
                r"^\[암기\]\s*\*\*암기\s*[—-]\s*",
                "[암기] ",
                study_item.group(1),
            )
            title_text = re.sub(
                r"^\[주의\]\s*\*\*주의:\*\*\s*",
                "[주의] ",
                title_text,
            )
            title_text = title_text.replace("**", "")
            title_text = re.sub(
                r"^\[암기\]\s*암기\s*:\s*",
                "[암기] ",
                title_text,
            )
            title_text = re.sub(
                r"^\[주의\]\s*주의\s*:\s*",
                "[주의] ",
                title_text,
            )
            index += 1
            detail_lines = []

            while index < len(lines):
                raw_detail = lines[index]
                detail = raw_detail.strip()
                indentation = len(raw_detail) - len(raw_detail.lstrip())

                if not detail:
                    index += 1
                    continue
                if indentation == 0:
                    break

                detail_bullet = re.match(r"^[-*+]\s+(.+)$", detail)
                if detail_bullet:
                    detail_lines.append(("item", detail_bullet.group(1)))
                else:
                    detail_lines.append(("note", detail))
                index += 1

            # 기존 형식처럼 글머리표를 먼저, 자유 문장을 뒤에 모은다.
            detail_lines.sort(key=lambda line: line[0] != "item")
            items = [content for kind, content in detail_lines if kind == "item"]
            notes = [content for kind, content in detail_lines if kind == "note"]

            # 페이지 나눔은 뒤쪽의 반복 배치 최적화가 담당하므로, 여기서는
            # 표를 나누지 않고 항상 하나로 묶어 한 주제로 보이게 한다.
            output.append(
                f'<table class="study-card {card_kind}"><tbody><tr><td>'
            )
            output.append(
                '<p class="study-title">'
                f'{_inline_markdown(title_text)}</p>'
            )
            if items:
                output.append('<ul class="study-details">')
                output.extend(
                    f"<li>{_inline_markdown(item)}</li>" for item in items
                )
                output.append("</ul>")
            output.extend(
                f'<p class="study-note">{_inline_markdown(note)}</p>'
                for note in notes
            )
            output.append("</td></tr></tbody></table>")
            continue

        bullet = re.match(r"^[-*+]\s+(.+)$", stripped)
        if bullet:
            close_paragraph()
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{_inline_markdown(bullet.group(1))}</li>")
            index += 1
            continue

        close_list()
        paragraph.append(stripped)
        index += 1

    close_paragraph()
    close_list()
    return "\n".join(output)


def _render_story_pdf(body_html):
    css = """
    @font-face { font-family: medstudy; src: url('AppleSDGothicNeo.ttc'); }
    body { font-family: medstudy; font-size: 10.5pt; line-height: 1.55; color: #20242c; }
    .math-inline, .math-display { font-family: medstudy; white-space: nowrap; }
    .math-display { display: block; text-align: center; margin: 7pt 0; font-size: 11pt; }
    .math-frac { white-space: nowrap; }
    sup, sub { font-size: 75%; line-height: 0; }
    h1 { font-size: 21pt; color: #182433; margin: 0 0 18pt 0; page-break-after: avoid; break-after: avoid; }
    h2 { font-size: 15pt; color: #1f4f78; margin: 22pt 0 8pt 0; padding-bottom: 4pt; border-bottom: 1px solid #c9d5df; page-break-after: avoid; break-after: avoid; }
    h3 { font-size: 12.5pt; color: #263f56; margin: 15pt 0 6pt 0; page-break-after: avoid; break-after: avoid; }
    h4 { font-size: 11.5pt; color: #34556f; margin: 12pt 0 5pt 0; page-break-after: avoid; break-after: avoid; }
    p { margin: 0 0 8pt 0; orphans: 2; widows: 2; }
    ul { margin: 2pt 0 9pt 17pt; padding: 0; }
    li { margin: 0 0 4pt 0; }
    blockquote { border-left: 4px solid #5c86a5; padding: 4pt 10pt; margin: 7pt 0 12pt 0; page-break-inside: avoid; break-inside: avoid; }
    .comparison-list { margin: 8pt 0 13pt 0; }
    .comparison-card { width: 100%; border-collapse: collapse; margin: 0 0 7pt 0; page-break-inside: avoid; break-inside: avoid; }
    .comparison-card tbody, .comparison-card tr, .comparison-card td { page-break-inside: avoid; break-inside: avoid; }
    .comparison-card td { border-left: 2px solid #9eb7ca; padding: 5pt 8pt; }
    .comparison-row { margin: 0 0 2.5pt 0; font-size: 9.3pt; }
    .comparison-label { color: #31546d; }
    /* 암기 카드는 표 블록으로 렌더링한다. MuPDF Story는 일반 div를
       페이지 중간에서 나눌 수 있지만, 이 표는 한 주제를 통째로 다음
       페이지로 넘겨 제목과 항목이 분리되지 않는다. */
    .study-card { width: 100%; border-collapse: collapse; margin: 8pt 0 12pt 0; page-break-inside: avoid; break-inside: avoid; }
    .study-card tbody, .study-card tr, .study-card td { page-break-inside: avoid; break-inside: avoid; }
    .study-card td { padding: 8pt 14pt; border-left: 4px solid #8ca8bd; }
    .study-card.warning td { border-left-color: #c99b55; }
    .study-title { margin: 0 0 7pt 0; font-weight: 600; color: #263f56; page-break-after: avoid; break-after: avoid; }
    .study-subtitle { margin: 7pt 0 4pt 8pt; font-weight: 600; color: #34556f; }
    .study-detail { margin: 0 0 4pt 10pt; }
    .study-detail.depth-1 { margin-left: 26pt; }
    .study-detail.depth-2 { margin-left: 42pt; }
    .study-details { margin: 2pt 0 1pt 17pt; page-break-inside: avoid; break-inside: avoid; }
    .study-note { margin: 4pt 0 0 0; color: #4e5963; }
    .qa-card { width: 100%; border-collapse: collapse; margin: 7pt 0 10pt 0; page-break-inside: avoid; break-inside: avoid; }
    .qa-card tbody, .qa-card tr, .qa-card td { page-break-inside: avoid; break-inside: avoid; }
    .qa-card td { border-left: 3px solid #6f95b2; padding: 6pt 9pt; }
    .qa-question { margin: 0 0 5pt 0; color: #263f56; }
    .qa-answer { margin: 0; padding-top: 4pt; border-top: 1px solid #d8e0e6; }
    .numbered-item { margin: 4pt 0 6pt 0; }
    .past-exam-highlight { background-color: #EED1FF; color: #20242c; padding: 1pt 2pt; }
    code { font-family: medstudy; color: inherit; font-size: inherit; }
    .figure-table { width: 100%; border-collapse: collapse; margin: 12pt 0 14pt 0; page-break-inside: avoid; break-inside: avoid; }
    .figure-table td { padding: 0; text-align: center; page-break-inside: avoid; break-inside: avoid; }
    img { object-fit: contain; }
    .figure-caption { color: #5d6873; font-size: 8.5pt; margin: 4pt 0 0 0; }
    .heading-figure { width: 100%; border-collapse: collapse; margin: 0 0 12pt 0; page-break-inside: avoid; break-inside: avoid; }
    .heading-figure td { padding: 0; text-align: center; page-break-inside: avoid; break-inside: avoid; }
    .heading-figure h3, .heading-figure h4 { text-align: left; }
    .heading-intro { width: 100%; border-collapse: collapse; margin: 0; page-break-inside: avoid; break-inside: avoid; }
    .heading-intro td { padding: 0; }
    .heading-intro h3, .heading-intro h4 { margin-bottom: 6pt; }
    /* 표 안의 blockquote가 자기 margin-bottom을 유지하면, 표를 벗어난
       뒤 세부 bullet 목록과의 사이에 불필요하게 큰 간격이 생긴다. */
    .heading-intro blockquote { margin-bottom: 0; }
    .exam-card-details { margin: 2pt 0 9pt 17pt; padding: 0; }
    .exam-card-details li { margin: 0 0 4pt 0; page-break-inside: avoid; break-inside: avoid; }
    """
    archive = pymupdf.Archive(str(FONT_DIR))
    story = pymupdf.Story(
        html=f"<html><body>{body_html}</body></html>",
        user_css=css,
        archive=archive,
    )
    stream = BytesIO()
    writer = pymupdf.DocumentWriter(stream)
    page_rect = pymupdf.paper_rect("a4")
    content_rect = pymupdf.Rect(48, 45, page_rect.width - 48, page_rect.height - 45)
    more = True

    while more:
        device = writer.begin_page(page_rect)
        more, _ = story.place(content_rect)
        story.draw(device)
        writer.end_page()

    writer.close()
    return stream.getvalue(), page_rect


def _find_squeezed_image_indexes(raw_pdf, minimum_size=100):
    """가로·세로가 모두 작아진 경우만 비정상 축소로 판단한다.

    기출 문제처럼 원래 가로로 긴 크롭은 정상 높이가 100pt보다 작을 수 있다.
    한쪽 길이만으로 판정하면 이런 이미지를 다음 페이지로 계속 밀어 큰 여백을 만든다.
    """
    document = pymupdf.open(stream=raw_pdf, filetype="pdf")
    squeezed = set()
    image_index = 0

    for page in document:
        for image_info in page.get_images(full=True):
            for image_rect in page.get_image_rects(image_info[0]):
                image_index += 1
                if (
                    image_rect.width < minimum_size
                    and image_rect.height < minimum_size
                ):
                    squeezed.add(image_index)

    document.close()
    return squeezed


def _get_image_captions(summary, lecture_pages, lecture_figures, exam_figures=None):
    """정리본에 삽입된 그림 캡션을 출력 순서대로 반환한다."""
    captions = []
    summary = _normalize_pdf_symbols(summary)

    for line in summary.splitlines():
        marker = IMAGE_MARKER.fullmatch(line.strip())
        if not marker:
            continue

        image_path, source = _resolve_image(
            marker.group(1),
            marker.group(2).strip(),
            lecture_pages,
            lecture_figures,
            exam_figures,
        )
        if image_path and Path(image_path).exists():
            captions.append(
                _normalize_layout_text(
                    f"{source} - {marker.group(3).strip()}"
                )
            )

    return captions


def _find_split_caption_indexes(raw_pdf, captions):
    """앞뒤 페이지로 갈라진 그림 캡션의 그림 순번을 찾는다."""
    document = pymupdf.open(stream=raw_pdf, filetype="pdf")
    page_texts = []
    for page in document:
        page_text = re.sub(
            r"\b\d+\s*/\s*\d+\b",
            "",
            page.get_text(),
        )
        page_texts.append(_normalize_layout_text(page_text))
    document.close()
    split_indexes = set()

    for image_index, caption in enumerate(captions, start=1):
        if any(caption in page_text for page_text in page_texts):
            continue

        for page_index in range(len(page_texts) - 1):
            previous_tail = page_texts[page_index][-len(caption) - 20:]
            next_head = page_texts[page_index + 1][:len(caption) + 20]

            for split_position in range(8, len(caption)):
                if (
                    previous_tail.endswith(caption[:split_position])
                    and next_head.startswith(caption[split_position:])
                ):
                    split_indexes.add(image_index)
                    break

            if image_index in split_indexes:
                break

    return split_indexes


def _normalize_layout_text(text):
    text = re.sub(r"[*_`]", "", text)
    text = text.replace("\xa0", " ")
    text = text.replace("・", "·")
    return re.sub(r"\s+", " ", text).strip()


def _get_markdown_headings(summary):
    summary = remove_accidental_strikethrough(summary)
    summary = group_repeated_summary_labels(summary)
    summary = _normalize_pdf_symbols(summary)
    return [
        _normalize_layout_text(match.group(1))
        for line in summary.splitlines()
        if (match := re.match(r"^#{1,4}\s+(.+)$", line.strip()))
    ]


def _find_orphan_heading_indexes(raw_pdf, headings, minimum_following_chars=45):
    """페이지 아래에 제목만 남은 Markdown 제목의 순번을 찾는다."""
    document = pymupdf.open(stream=raw_pdf, filetype="pdf")
    orphaned = set()
    page_blocks = []

    for page in document:
        blocks = sorted(
            page.get_text("blocks"),
            key=lambda block: (block[1], block[0]),
        )
        page_blocks.append((page.rect.height, blocks))

    for heading_index, expected_heading in enumerate(headings, start=1):
        matched = None

        for page_height, blocks in page_blocks:
            for block_index, block in enumerate(blocks):
                block_text = _normalize_layout_text(block[4])
                if block_text == expected_heading:
                    matched = (page_height, blocks, block_index, block)
                    break
            if matched:
                break

        if not matched:
            continue

        page_height, blocks, block_index, block = matched
        following_text = " ".join(
            _normalize_layout_text(candidate[4])
            for candidate in blocks[block_index + 1:]
            if candidate[1] > block[3]
            and candidate[3] < page_height - 28
        )
        following_text = re.sub(
            r"\b\d+\s*/\s*\d+\b",
            "",
            following_text,
        ).strip()

        # 기출 카드(🎯)는 제목과 문제 요점만 남고 핵심 개념부터
        # 다음 페이지로 밀리면 읽는 흐름이 끊긴다. 핵심 개념까지
        # 같은 페이지에 시작하도록 제목 전체를 다음 페이지로 이동한다.
        is_exam_card = "🎯" in expected_heading
        exam_card_has_core = "필요한 핵심 개념" in following_text

        if len(following_text) < minimum_following_chars or (
            is_exam_card and not exam_card_has_core
        ):
            orphaned.add(heading_index)

    document.close()
    return orphaned


def _get_qa_pairs(summary):
    lines = summary.splitlines()
    qa_pairs = []

    for index, line in enumerate(lines):
        question_match = re.match(r"^(\d+)\.\s+(.+)$", line.strip())
        if not question_match:
            continue

        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1

        if next_index >= len(lines):
            continue

        answer_match = re.match(
            r"^\*\*정답:\*\*\s*(.+)$",
            lines[next_index].strip(),
        )
        if answer_match:
            qa_pairs.append(
                (
                    _normalize_layout_text(question_match.group(2)),
                    _normalize_layout_text(answer_match.group(1)),
                )
            )

    return qa_pairs


def _find_split_qa_indexes(raw_pdf, qa_pairs):
    """질문과 답이 서로 다른 페이지에 놓인 체크리스트 문항을 찾는다."""
    document = pymupdf.open(stream=raw_pdf, filetype="pdf")
    page_texts = [
        _normalize_layout_text(page.get_text())
        for page in document
    ]
    document.close()
    split_indexes = set()

    for qa_index, (question, _answer) in enumerate(qa_pairs, start=1):
        question_marker = f"Q{qa_index}. {question}"
        question_page = next(
            (
                page_text
                for page_text in page_texts
                if question_marker in page_text
            ),
            None,
        )
        if not question_page:
            continue

        question_position = question_page.find(question_marker)
        answer_label_position = question_page.find(
            "정답",
            question_position + len(question_marker),
        )

        if answer_label_position < 0:
            split_indexes.add(qa_index)
            continue

        answer_tail = question_page[
            answer_label_position + len("정답"):
        ]
        answer_tail = re.sub(
            r"\b\d+\s*/\s*\d+\b.*$",
            "",
            answer_tail,
        ).strip()

        if len(answer_tail) < 2:
            split_indexes.add(qa_index)

    return split_indexes


def _layout_quality_score(raw_pdf, headings):
    """과도한 빈 공간과 고아 제목을 함께 평가한다. 낮을수록 좋다."""
    document = pymupdf.open(stream=raw_pdf, filetype="pdf")
    whitespace_penalty = 0

    for page_index, page in enumerate(document):
        # 마지막 페이지는 내용이 짧은 것이 자연스러우므로 여백 벌점을 주지 않는다.
        if page_index == len(document) - 1:
            continue
        body_blocks = [
            block for block in page.get_text("blocks")
            if block[1] < page.rect.height - 35
            and not re.fullmatch(
                r"\s*\d+\s*/\s*\d+\s*",
                block[4],
            )
        ]
        content_bottom = max(
            (block[3] for block in body_blocks),
            default=45,
        )
        blank_height = max(0, page.rect.height - 45 - content_bottom)
        # 약 110pt 이하는 정상적인 문단/카드 나눔 여유로 본다. 그보다
        # 큰 빈 공간은 제곱 벌점으로 처리해 반쪽짜리 페이지를 강하게 피한다.
        whitespace_penalty += max(0, blank_height - 110) ** 2

    page_count = len(document)
    document.close()
    orphan_count = len(_find_orphan_heading_indexes(raw_pdf, headings))
    return whitespace_penalty + orphan_count * 8000 + page_count * 2000


def create_summary_pdf(summary, lecture_pages, lecture_figures, exam_figures=None):
    """저장된 Markdown 정리본을 한글 A4 PDF 바이트로 변환한다."""
    # 이 함수 안에서만 반복되는 배치 최적화 과정(최대 6회 + 여백 제거
    # 시도)이 같은 이미지를 매번 다시 열고 리사이즈하지 않도록 캐시를
    # 쓴다. 대신 호출마다 캐시를 비워, 다른 강의를 내보내거나 같은
    # 강의를 다시 내보낼 때(이미지가 새로 만들어졌을 수 있음) 항상
    # 디스크의 최신 파일을 읽는다.
    _image_data_uri.cache_clear()
    _image_display_size.cache_clear()
    page_break_before_images = set()
    page_break_before_headings = set()
    page_break_before_questions = set()
    headings = _get_markdown_headings(summary)
    qa_pairs = _get_qa_pairs(summary)
    for _ in range(6):
        body_html = _summary_to_html(
            summary,
            lecture_pages,
            lecture_figures,
            exam_figures,
            page_break_before_images,
            page_break_before_headings,
            page_break_before_questions,
        )
        raw_pdf, page_rect = _render_story_pdf(body_html)
        squeezed = _find_squeezed_image_indexes(raw_pdf)
        new_squeezed = squeezed - page_break_before_images
        orphaned = _find_orphan_heading_indexes(raw_pdf, headings)
        new_orphaned = orphaned - page_break_before_headings
        split_questions = _find_split_qa_indexes(raw_pdf, qa_pairs)
        new_split_questions = (
            split_questions - page_break_before_questions
        )

        if (
            not new_squeezed
            and not new_orphaned
            and not new_split_questions
        ):
            break

        page_break_before_images.update(new_squeezed)
        page_break_before_headings.update(new_orphaned)
        page_break_before_questions.update(new_split_questions)

    # 앞선 반복에서 필요했던 그림 강제 넘김이 이후 재배치로 불필요해질
    # 수 있다. 최종 상태에서 하나씩 제거해도 그림 축소나 다른 페이지
    # 나눔 문제가 생기지 않는 항목은 제거하여 빈 여백을 줄인다.
    for image_index in sorted(page_break_before_images):
        candidate_image_breaks = page_break_before_images - {image_index}
        candidate_html = _summary_to_html(
            summary,
            lecture_pages,
            lecture_figures,
            exam_figures,
            candidate_image_breaks,
            page_break_before_headings,
            page_break_before_questions,
        )
        candidate_pdf, _ = _render_story_pdf(candidate_html)
        if _find_squeezed_image_indexes(candidate_pdf):
            continue
        if (
            _find_orphan_heading_indexes(candidate_pdf, headings)
            - page_break_before_headings
        ):
            continue
        if (
            _find_split_qa_indexes(candidate_pdf, qa_pairs)
            - page_break_before_questions
        ):
            continue

        page_break_before_images = candidate_image_breaks
        raw_pdf = candidate_pdf

    # 제목 강제 넘김은 앞쪽 내용을 이동시켜 다음 제목까지 연쇄적으로
    # 넘기는 부작용이 있다. 질문/정답과 그림 안전성은 유지하면서, 각 제목
    # 넘김을 제거한 후보의 전체 여백 점수가 좋아지면 제거한다.
    improved = True
    while improved and page_break_before_headings:
        improved = False
        current_score = _layout_quality_score(raw_pdf, headings)
        for heading_index in sorted(page_break_before_headings):
            candidate_heading_breaks = (
                page_break_before_headings - {heading_index}
            )
            candidate_html = _summary_to_html(
                summary,
                lecture_pages,
                lecture_figures,
                exam_figures,
                page_break_before_images,
                candidate_heading_breaks,
                page_break_before_questions,
            )
            candidate_pdf, _ = _render_story_pdf(candidate_html)
            if _find_squeezed_image_indexes(candidate_pdf):
                continue
            if _find_split_qa_indexes(candidate_pdf, qa_pairs):
                continue
            candidate_score = _layout_quality_score(candidate_pdf, headings)
            if candidate_score < current_score:
                page_break_before_headings = candidate_heading_breaks
                raw_pdf = candidate_pdf
                current_score = candidate_score
                improved = True
                break

    document = pymupdf.open(stream=raw_pdf, filetype="pdf")
    for page_no, page in enumerate(document, start=1):
        page.insert_text(
            (page_rect.width / 2 - 12, page_rect.height - 20),
            f"{page_no} / {len(document)}",
            fontsize=8,
            color=(0.38, 0.42, 0.46),
        )

    final_stream = BytesIO()
    document.save(final_stream)
    document.close()
    return final_stream.getvalue()
