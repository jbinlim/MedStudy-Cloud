"""강의록·과목 기출 PDF에서 정리본에 넣을 원문 문제 이미지를 준비한다."""

import io
import re
import unicodedata
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image


EXAM_PATTERN = re.compile(
    r"(?:기출|형성평가|중간(?:고사)?|기말(?:고사)?|객\s*\d+|"
    r"20\d{2}\s*(?:년도|년)?.{0,20}(?:문항|문제|중간|기말))",
    re.IGNORECASE,
)
PAGE_SECTION = re.compile(
    r"\[기출 PDF\s+(\d+)페이지\]\s*(.*?)(?=\n\n\[기출 PDF|\Z)",
    re.DOTALL,
)
ANSWER_FILE_PATTERN = re.compile(r"(?:정답|답지|해설|answer)", re.IGNORECASE)
# 문항 영역과 별개로 강의록에 덧붙은 정답 공개 구간(예: `<정답>`, `정답: 3번`,
# `*25 중간 객2: 5번(추정)`)만 가리킨다. 선택지 문구인 `⑤ 정답없음`처럼 문항
# 번호/기호로 시작하는 줄은 걸러내 정상 선택지까지 잘라내지 않게 한다.
ANSWER_BLOCK_LINE_PATTERN = re.compile(
    r"^\s*(?:<\s*정답\s*>|정답\s*[:：]|\*[^:：\n]{0,40}[:：]\s*\d+\s*번)"
)
# `21 중간(정주원) 객4`처럼 문항 상자 위에 붙는 출처 라벨. 문항 자체가
# OCR로 읽히지 않는 그림(사진)이라 문항 시작으로 잡히지 않아도, 이 라벨은
# 텍스트로 남아 있어 "다음 문항이 시작되는 지점"을 알려 준다.
EXAM_SOURCE_LABEL_PATTERN = re.compile(
    r"^\s*\d{2}\s*(?:중간|기말)(?:\([^)]{0,12}\))?\s*(?:고사)?\s*객\s*\d+"
)
CHOICE_PATTERN = re.compile(
    r"(?:[①②③④⑤⑥]|[@©®]|\b[A-E]\s*[.)]|(?<!\d)[1-5]\s*[.)])",
    re.IGNORECASE,
)
OCR_CHOICE_LINE_PATTERN = re.compile(
    r"(?m)^\s*(?:[①②③④⑤⑥@©®]|[A-E]\s*[.)]|\(?[1-5]\)?\s+)",
    re.IGNORECASE,
)
OX_QUESTION_PATTERN = re.compile(
    r"(?:\[\s*\]|O\s*[,/·]\s*X|X\s*[,/·]\s*O|"
    r"옳으면.{0,12}틀리면|참.{0,12}거짓|관련한?\s*기술)",
    re.IGNORECASE,
)
COVER_HISTORY_PATTERN = re.compile(
    r"(?:누락|출제\s*횟수|출제\s*이력|\d{2}\s*(?:중간|기말)\s*[가-힣]{2,4})",
    re.IGNORECASE,
)


def _normalized(value):
    return unicodedata.normalize("NFC", str(value or ""))


def _year_key(file_name):
    name = _normalized(file_name)
    full = re.search(r"(?<!\d)(20\d{2})(?!\d)", name)
    if full:
        return int(full.group(1))
    short = re.search(r"(?<!\d)(\d{2})(?:\s*[-_]\s*[12])?(?!\d)", name)
    if short:
        value = int(short.group(1))
        return 2000 + value if value < 80 else 1900 + value
    return 9999


def _tokens(text):
    return {
        token.lower()
        for token in re.findall(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9+-]{2,}", text or "")
        if token.lower() not in {"그리고", "대한", "에서", "으로", "the", "and", "with"}
    }


def _page_text_map(extracted_text):
    return {
        int(match.group(1)): match.group(2).strip()
        for match in PAGE_SECTION.finditer(extracted_text or "")
    }


def _content_clip(page):
    rects = []
    for block in page.get_text("blocks"):
        if str(block[4]).strip():
            rects.append(pymupdf.Rect(block[:4]))
    for image in page.get_images(full=True):
        rects.extend(page.get_image_rects(image[0]))
    if not rects:
        return page.rect
    union = rects[0]
    for rect in rects[1:]:
        union |= rect
    padded = pymupdf.Rect(
        max(page.rect.x0, union.x0 - 16),
        max(page.rect.y0, union.y0 - 16),
        min(page.rect.x1, union.x1 + 16),
        min(page.rect.y1, union.y1 + 16),
    )
    return padded if padded.width > 80 and padded.height > 80 else page.rect


def _render_page(page, destination, clip=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(2, 2),
        clip=clip or _content_clip(page),
        alpha=False,
    )
    pixmap.save(destination)


def _render_composite(page, destination, clips):
    """공통 제시문·도판과 개별 문항을 한 이미지로 이어 붙인다."""
    rendered = []
    for clip in clips:
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(2, 2), clip=clip, alpha=False
        )
        rendered.append(Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB"))
    if not rendered:
        return
    width = max(image.width for image in rendered)
    canvas = Image.new("RGB", (width, sum(image.height for image in rendered)), "white")
    y = 0
    for image in rendered:
        canvas.paste(image, (0, y))
        y += image.height
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, optimize=True)


def _shared_question_regions(page):
    """`10-11.`처럼 여러 문항이 공유하는 제시문·도판 영역을 찾는다."""
    tessdata = Path(__file__).resolve().parent / "ocr_data"
    try:
        textpage = page.get_textpage_ocr(
            language="kor+eng", dpi=150, full=True, tessdata=str(tessdata)
        )
        words = page.get_text("words", textpage=textpage)
    except Exception:
        words = page.get_text("words")
    grouped = {}
    for word in words:
        grouped.setdefault((word[5], word[6]), []).append(word)
    lines = []
    for line_words in grouped.values():
        line_words.sort(key=lambda item: item[0])
        text = " ".join(str(item[4]) for item in line_words).strip()
        lines.append((
            min(item[1] for item in line_words),
            min(item[0] for item in line_words),
            text,
        ))
    lines.sort()
    middle = (page.rect.x0 + page.rect.x1) / 2
    result = {}
    for y0, x0, text in lines:
        match = re.match(r"^(\d{1,3})\s*[-–~]\s*(\d{1,3})\s*[.,]", text)
        if not match:
            continue
        first, last = int(match.group(1)), int(match.group(2))
        if first >= last or last > 200:
            continue
        column = 0 if x0 < middle else 1
        individual_starts = []
        for line_y, line_x, line_text in lines:
            same_column = (line_x < middle) == (column == 0)
            number = re.match(r"^(\d{1,3})\s*[.,]\s+", line_text)
            if same_column and number and line_y > y0 + 2:
                value = int(number.group(1))
                if first <= value <= last:
                    individual_starts.append(line_y)
        if not individual_starts:
            continue
        clip = pymupdf.Rect(
            page.rect.x0 if column == 0 else middle + 4,
            max(page.rect.y0, y0 - 7),
            middle - 4 if column == 0 else page.rect.x1,
            min(individual_starts) - 5,
        )
        if clip.height >= 40:
            for question_no in range(first, last + 1):
                result[str(question_no)] = clip
    return result


def _pixmap_has_visible_content(document, xref, minimum_ratio=0.004):
    """거의 흰색뿐인 PowerPoint 배경 이미지를 문항으로 고르지 않는다."""
    pixmap = pymupdf.Pixmap(document, xref)
    channels = pixmap.n
    color_channels = min(3, channels)
    pixel_count = max(1, pixmap.width * pixmap.height)
    # 픽셀마다 파이썬 루프를 도는 대신 numpy로 한 번에 계산한다. 큰
    # 임베디드 이미지(수백만 픽셀)에서 순수 파이썬 루프는 초 단위로
    # 느려져, 기출 페이지가 많은 강의에서 눈에 띄는 지연을 만든다.
    samples = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(-1, channels)
    darkest = samples[:, :color_channels].min(axis=1)
    visible = int((darkest < 240).sum())
    light = int((darkest > 245).sum())
    return (
        visible / pixel_count >= minimum_ratio
        and light / pixel_count >= 0.20
    )


def _ocr_embedded_image(page, rect, zoom=2):
    """실제로 크롭해 저장할 화면 그대로를 OCR해 주변 슬라이드 글자 혼입을 막는다.

    PowerPoint에서 자른(crop) 사진은 XObject 원본 픽셀에 잘려나간
    부분이 그대로 남아 있을 수 있다. `pymupdf.Pixmap(document, xref)`로
    원본 이미지 객체를 직접 OCR하면, 페이지에는 잘려서 안 보이는
    부분(예: 문제 앞부분)까지 읽혀 "완전한 문항"으로 잘못 판정하고,
    정작 저장되는 크롭 이미지는 잘린 채로 나간다. 반드시 저장에 쓰는
    것과 같은 `page.get_pixmap(clip=rect)` 렌더링 결과를 OCR해야
    화면에 실제로 보이는 내용과 판정이 일치한다.

    `zoom`은 기본 2배(문항 상자처럼 충분히 큰 영역용)이며, 문항 위
    작은 출처 라벨처럼 세로 폭이 좁은 영역은 이 배율로는 Tesseract가
    "너무 작다"며 아예 인식을 포기해 빈 문자열만 나온다. 그런 영역은
    호출하는 쪽에서 더 큰 배율을 넘긴다.
    """
    tessdata = Path(__file__).resolve().parent / "ocr_data"
    try:
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect, alpha=False)
        pdf_bytes = pixmap.pdfocr_tobytes(
            language="kor+eng",
            tessdata=str(tessdata),
        )
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document_ocr:
            return document_ocr[0].get_text("text").strip()
    except Exception:
        return ""


def _question_regions(page, embedded_regions=None):
    """텍스트/OCR 좌표에서 문항별 영역을 찾는다. 실패하면 빈 목록을 반환한다."""
    textpage = None
    if len(page.get_text("text").strip()) < 80:
        tessdata = Path(__file__).resolve().parent / "ocr_data"
        try:
            textpage = page.get_textpage_ocr(
                language="kor+eng", dpi=150, full=True, tessdata=str(tessdata)
            )
        except Exception:
            textpage = None
    words = page.get_text("words", textpage=textpage) if textpage else page.get_text("words")
    # 숫자 하나만 보고 문항 시작으로 판단하면 Canvas의 점수(1 / 1점),
    # 선택지 번호, 페이지 번호까지 문항으로 오인한다. 같은 텍스트 줄을 먼저
    # 복원한 뒤 `문제 N` 또는 왼쪽에서 시작하는 `N.` 형식만 사용한다.
    grouped_lines = {}
    for word in words:
        grouped_lines.setdefault((word[5], word[6]), []).append(word)

    lines = []
    for line_words in grouped_lines.values():
        line_words.sort(key=lambda item: item[0])
        text = " ".join(str(item[4]) for item in line_words).strip()
        rect = pymupdf.Rect(
            min(item[0] for item in line_words),
            min(item[1] for item in line_words),
            max(item[2] for item in line_words),
            max(item[3] for item in line_words),
        )
        lines.append((rect.y0, rect.x0, text, rect))
    lines.sort()

    starts = []
    for _, x0, text, rect in lines:
        match = re.match(r"^문제\s*(\d{1,3})\b", text)
        if not match:
            # 시험지의 오른쪽 열도 찾되 `1)` 같은 선택지 번호는 문항
            # 시작으로 오인하지 않도록 점(.)으로 시작하는 번호만 받는다.
            match = re.match(r"^(\d{1,3})\s*[.,]\s+", text)
        if not match:
            continue
        question_value = int(match.group(1))
        if 1 <= question_value <= 200 and rect.y0 <= page.rect.y1 - 30:
            starts.append((str(question_value), rect.y0, x0))
    starts = list(dict.fromkeys(starts))
    if not starts:
        return []

    content_clip = _content_clip(page)
    page_middle = (page.rect.x0 + page.rect.x1) / 2

    # 옆 문항이 사진(그림)이라 OCR로 선택지까지는 못 읽어도 맨 앞 번호
    # (`6.`, `13.` 등)만은 읽히는 경우가 있다. 이런 사진을 놓치면 2단
    # 배치를 못 알아채고 반대쪽 문항 사진까지 폭 전체를 크롭에 포함한다.
    # 번호가 읽히는 사진만 보조 시작점으로 취급해 열 구분에 반영한다.
    image_starts = []
    seen_xrefs = set()
    for image in page.get_images(full=True):
        xref = image[0]
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        try:
            if not _pixmap_has_visible_content(page.parent, xref):
                continue
        except Exception:
            continue
        for rect in page.get_image_rects(xref):
            if rect.width < 100 or rect.height < 40:
                continue
            ocr_text = _ocr_embedded_image(page, rect)
            match = re.match(r"\s*(\d{1,3})\s*[.,]", ocr_text)
            if not match:
                continue
            value = int(match.group(1))
            if 1 <= value <= 200:
                image_starts.append((str(value), rect.y0, rect.x0))
    image_starts = list(dict.fromkeys(image_starts))

    # 2단 시험지는 왼쪽·오른쪽 문항이 같은 높이에 배치된다. 시작점이
    # 페이지 양쪽에 충분히 존재하면 열별로 독립된 문항 흐름으로 나눈다.
    # 번호가 읽히는 문항 사진처럼 확실한 근거가 한쪽에 있으면, 반대쪽에
    # 문항이 하나뿐이어도 2단으로 본다.
    left_starts = [item for item in starts if item[2] < page_middle]
    right_starts = [item for item in starts if item[2] >= page_middle]
    left_image_starts = [item for item in image_starts if item[2] < page_middle]
    right_image_starts = [item for item in image_starts if item[2] >= page_middle]
    two_columns = (
        (len(left_starts) >= 2 and len(right_starts) >= 2)
        or (bool(left_image_starts) and bool(right_starts or right_image_starts))
        or (bool(right_image_starts) and bool(left_starts or left_image_starts))
    )
    # 페이지 전체 OCR(문항 번호를 `starts`로도 찾음)과 그림별 개별 OCR
    # (`image_starts`)이 같은 사진 문항의 시작 번호를 좌표만 살짝 다르게
    # (측정 오차) 두 번 잡아낼 수 있다. 좌표까지 비슷한 중복은 먼저
    # 하나로 합쳐, 아래 열 재배정 단계에서 같은 문항의 두 사본이 서로
    # 다른 열로 갈리는 것을 막는다.
    combined_starts = []
    for entry in starts + image_starts:
        entry_no, entry_y, entry_x = entry
        if any(
            entry_no == seen_no and abs(entry_y - seen_y) < 25 and abs(entry_x - seen_x) < 25
            for seen_no, seen_y, seen_x in combined_starts
        ):
            continue
        combined_starts.append(entry)

    starts_with_columns = [
        (*item, 0 if not two_columns or item[2] < page_middle else 1)
        for item in combined_starts
    ]
    # 오른쪽 열 문항이 여백 없이 페이지 중앙 바로 옆에서 시작하면(예:
    # x=411.8, 중앙=420.9) 고정된 중앙값 비교만으로는 왼쪽 열로 잘못
    # 묶인다. 같은 열로 분류된 두 시작점이 세로로 거의 같은 줄(±15pt)에
    # 있으면 실제로는 서로 다른 열의 문항이 우연히 같은 높이에서
    # 시작한 것일 수 있다. 다만 둘 다 중앙값에서 멀리 떨어져 있다면
    # (예: 둘 다 왼쪽 깊숙이) 같은 열 안의 서로 다른 겹침일 뿐이므로,
    # 중앙값에 가까워 원래 분류가 애매했던 쪽만 반대 열로 재배정한다.
    if two_columns:
        starts_with_columns.sort(key=lambda item: item[1])
        for index, item in enumerate(starts_with_columns):
            for other in starts_with_columns[index + 1:]:
                if other[1] > item[1] + 15:
                    break
                if (
                    other[3] == item[3]
                    and other[2] > item[2] + 15
                    and abs(other[2] - page_middle) <= 20
                ):
                    other_index = starts_with_columns.index(other)
                    starts_with_columns[other_index] = (*other[:3], 1 - other[3])
    # 다음 문항 시작을 못 찾으면 영역이 페이지 하단까지 늘어나 강의록에
    # 별도로 붙은 정답 공개 구간(`<정답>`, `정답: 3번` 등)이나 옆·아래의
    # 다른 문항 상자(그림이라 `starts`로 못 잡힌 경우)까지 크롭에 딸려
    # 들어온다. 두 종류의 경계 줄을 모두 찾아 각 문항 영역의 하단을
    # 그 앞에서 반드시 끊는다.
    answer_cutoffs = [
        (rect.y0, x0)
        for y0, x0, text, rect in lines
        if ANSWER_BLOCK_LINE_PATTERN.match(text) or EXAM_SOURCE_LABEL_PATTERN.match(text)
    ]
    regions = []
    for question_no, y0, _, column in starts_with_columns:
        if two_columns and column == 0:
            clip_left, clip_right = content_clip.x0, page_middle - 4
        elif two_columns:
            clip_left, clip_right = page_middle + 4, content_clip.x1
        else:
            clip_left, clip_right = content_clip.x0, content_clip.x1
        following = [
            item[1]
            for item in starts_with_columns
            if item[3] == column and item[1] > y0 + 1
        ]
        cutoffs = [
            cutoff_y
            for cutoff_y, cutoff_x in answer_cutoffs
            if cutoff_y > y0 + 1
            and (not two_columns or (cutoff_x < page_middle) == (column == 0))
        ]
        # 다음 문항 시작이 없어 영역이 페이지 하단까지 늘어나면, 같은
        # 가로 범위에 걸치는 사진(그림) 문항의 위쪽 경계까지도 잘라내야
        # 서로 다른 문항이 한 크롭에 섞이지 않는다. `starts`로는 안 잡힌
        # 그림 문항(예: 폭이 두 칼럼을 가로지르는 이미지)이 대상이다.
        image_cutoffs = [
            rect.y0
            for _, rect, _, _ in (embedded_regions or [])
            if rect.y0 > y0 + 1 and rect.x1 > clip_left and rect.x0 < clip_right
        ]
        next_y = min([*following, *cutoffs, *image_cutoffs, content_clip.y1])
        clip = pymupdf.Rect(
            max(page.rect.x0, clip_left - 4),
            max(page.rect.y0, y0 - 7),
            min(page.rect.x1, clip_right + 4),
            min(page.rect.y1, next_y - 5),
        )
        if clip.width < 80 or clip.height < 38:
            continue
        region_words = [
            word for word in words
            if clip.contains(
                pymupdf.Point((word[0] + word[2]) / 2, (word[1] + word[3]) / 2)
            )
        ]
        region_words.sort(key=lambda word: (word[5], word[6], word[7]))
        text = " ".join(str(word[4]) for word in region_words).strip()
        regions.append((question_no, clip, text))
    return regions


def _looks_like_complete_question(text):
    """표지 출제 이력이 아니라 실제 질문·선택지가 있는 영역인지 확인한다."""
    compact = " ".join((text or "").split())
    if len(compact) < 45:
        return False
    choice_count = max(
        len(CHOICE_PATTERN.findall(compact)),
        len(OCR_CHOICE_LINE_PATTERN.findall(text or "")),
    )
    # `것은`, `무엇`, `해당하는`, 홀로 쓰인 `옳은`/`고르`는 일반 강의
    # 설명문에도 흔히 등장해, 슬라이드가 그림 설명에 ①②③ 같은 원문자
    # 목록만 곁들여도 기출 문항으로 오인하게 만든다. 실제 시험 문구에서만
    # 나타나는 조합만 남긴다.
    # OCR은 `옳`을 `옮`으로 잘못 읽거나 `고르시오`를 줄바꿈으로
    # `고르\n시오`처럼 갈라놓는 경우가 흔해, 문자 하나 차이로 실제
    # 문항을 놓치지 않도록 그 변형까지 허용한다.
    has_question_wording = bool(
        re.search(
            r"(?:\?|다음\s*중|[옳옮]지\s*않은|맞는\s*문장|"
            r"고르\s*시오|고르\s*세요|가장\s*[옳옮]은|가장\s*적절|어느\s*것|"
            r"(?:어디|무엇|언제|누구|몇)\s*인가)",
            compact,
        )
    )
    has_ox_format = bool(OX_QUESTION_PATTERN.search(compact))
    # 실제 질문과 선택지가 모두 있으면 영역 안의 `22 중간 객1` 같은
    # 시험 표기는 정상적인 문항 메타데이터다. 질문/선택지가 없는 경우에만
    # 표지의 출제 이력으로 제외한다.
    if has_question_wording and choice_count >= 2:
        return True
    if has_ox_format and choice_count >= 2:
        return True
    return False if COVER_HISTORY_PATTERN.search(compact) else False


PURPLE_LABEL_COLOR = (210, 135, 255)
PURPLE_COLOR_TOLERANCE = 40
PURPLE_BOX_MIN_WIDTH = 150
PURPLE_BOX_MIN_HEIGHT = 80
PURPLE_BOX_MAX_FILL_RATIO = 0.2
PURPLE_LABEL_STRIP_HEIGHT = 44
FULL_PAGE_AREA_RATIO = 0.85


def _connected_purple_components(mask):
    """보라색 픽셀 덩어리를 4방향 연결 기준으로 묶어 각 덩어리의 경계
    상자와 픽셀 수를 반환한다(외부 라이브러리 없이 numpy만으로 처리)."""
    visited = np.zeros_like(mask, dtype=bool)
    height, width = mask.shape
    components = []
    ys, xs = np.nonzero(mask)
    for start_y, start_x in zip(ys.tolist(), xs.tolist()):
        if visited[start_y, start_x]:
            continue
        stack = [(start_y, start_x)]
        visited[start_y, start_x] = True
        min_y = max_y = start_y
        min_x = max_x = start_x
        pixel_count = 0
        while stack:
            cy, cx = stack.pop()
            pixel_count += 1
            if cy < min_y:
                min_y = cy
            if cy > max_y:
                max_y = cy
            if cx < min_x:
                min_x = cx
            if cx > max_x:
                max_x = cx
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        components.append((min_y, min_x, max_y, max_x, pixel_count))
    return components


def _detect_purple_boxes(pil_image):
    """학생이 만든 족보 자료에서 문항을 감싼 보라색 사각형 테두리를 찾는다.

    여러 해의 기출 스크린샷을 한 슬라이드에 모아 만든 자료는 PDF 안에서
    개별 이미지 객체로 분리되지 않고 페이지 전체가 통짜 이미지 하나로
    저장된다. 이런 페이지는 임베디드 이미지 경계로는 문항을 나눌 수
    없지만, 이런 자료는 보통 항상 같은 보라색(RGB 210,135,255) 사각형
    으로 각 문항을 감싸 두므로, 그 색만 찾아 사각형 테두리(가늘고 성긴
    픽셀 뭉치만 남기고 글자처럼 작고 빽빽한 덩어리는 제외)로 개별 문항
    영역을 되찾는다.
    """
    array = np.array(pil_image.convert("RGB"))
    diff = np.abs(array.astype(np.int16) - np.array(PURPLE_LABEL_COLOR, dtype=np.int16))
    mask = np.all(diff <= PURPLE_COLOR_TOLERANCE, axis=-1)
    if not mask.any():
        return []

    boxes = []
    for min_y, min_x, max_y, max_x, pixel_count in _connected_purple_components(mask):
        width = max_x - min_x + 1
        height = max_y - min_y + 1
        if width < PURPLE_BOX_MIN_WIDTH or height < PURPLE_BOX_MIN_HEIGHT:
            continue
        fill_ratio = pixel_count / (width * height)
        if fill_ratio > PURPLE_BOX_MAX_FILL_RATIO:
            continue
        boxes.append((min_x, min_y, max_x, max_y))
    return boxes


LABEL_SOURCE_PATTERN = re.compile(
    r"(\d{2,4})\s*(중간|기말)\s*(?:\([^)]{0,12}\))?\s*(?:고사)?\s*객?\s*(\d{1,3})"
)


def _split_purple_labeled_regions(page, rect, xref):
    """한 이미지 안에 보라색 박스로 나뉜 여러 기출 문항이 있으면 쪼갠다.

    박스를 못 찾거나 하나뿐이면 빈 목록을 반환해, 호출하는 쪽에서 원래
    영역을 그대로 쓰게 한다.
    """
    try:
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=rect, alpha=False)
        pil_image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    except Exception:
        return []

    boxes = _detect_purple_boxes(pil_image)
    if len(boxes) < 2:
        return []

    # 렌더 배율(2x)을 되돌려 PDF 좌표로 바꾸고, 원래 클립 영역의
    # 원점(rect.x0, rect.y0)만큼 다시 더한다.
    box_rects = [
        pymupdf.Rect(
            rect.x0 + min_x / 2 - 2,
            rect.y0 + min_y / 2 - 2,
            rect.x0 + max_x / 2 + 2,
            rect.y0 + max_y / 2 + 2,
        )
        for min_x, min_y, max_x, max_y in boxes
    ]

    sub_regions = []
    for box_rect in sorted(box_rects, key=lambda value: (value.y0, value.x0)):
        # 라벨은 기본적으로 문항 상자 위 일정 높이까지 올려 잡지만,
        # 같은 열 바로 위에 다른 문항 상자가 있으면 그 아래쪽 경계를
        # 넘지 않게 제한한다. 안 그러면 위 문항의 마지막 선택지·테두리
        # 까지 함께 읽혀 라벨 OCR이 엉망이 된다(2x2 격자로 촘촘히 배치된
        # 경우 특히 자주 생김).
        label_top = max(rect.y0, box_rect.y0 - PURPLE_LABEL_STRIP_HEIGHT)
        for other_rect in box_rects:
            if other_rect is box_rect:
                continue
            overlaps_x = other_rect.x0 < box_rect.x1 and other_rect.x1 > box_rect.x0
            if overlaps_x and other_rect.y1 <= box_rect.y0:
                label_top = max(label_top, other_rect.y1)
        label_rect = pymupdf.Rect(box_rect.x0, label_top, box_rect.x1, box_rect.y0)
        label_text = (
            _ocr_embedded_image(page, label_rect, zoom=6)
            if label_rect.height > 4
            else ""
        )
        question_text = _ocr_embedded_image(page, box_rect)

        label_match = LABEL_SOURCE_PATTERN.search(label_text)
        if label_match:
            question_no = label_match.group(3)
            source_label = label_match.group(0).strip()
        else:
            question_match = re.match(r"\s*(\d{1,3})\s*[.)]?", question_text)
            question_no = question_match.group(1) if question_match else None
            source_label = label_text.strip()

        combined_text = (
            (source_label + "\n" if source_label else "") + question_text
        ).strip()
        sub_regions.append((question_no, box_rect, combined_text, xref))

    return sub_regions


def _embedded_question_regions(page):
    """슬라이드에 그림으로 붙은 원문 문제 박스만 찾아 정답 텍스트와 분리한다."""
    image_rects = []
    seen_xrefs = set()
    for image in page.get_images(full=True):
        if image[0] in seen_xrefs:
            continue
        seen_xrefs.add(image[0])
        if not _pixmap_has_visible_content(page.parent, image[0]):
            continue
        for rect in page.get_image_rects(image[0]):
            # OX 진술은 A·B가 높이 10~20pt의 별도 이미지 조각으로
            # 저장되기도 하므로 작은 가로형 텍스트 조각도 병합 후보로 둔다.
            if rect.width >= 100 and rect.height >= 10:
                image_rects.append((image[0], rect))
    if not image_rects:
        return []

    image_parts = [
        (xref, rect, _ocr_embedded_image(page, rect))
        for xref, rect in image_rects
    ]
    regions = []
    for xref, rect, text in image_parts:
        if not _looks_like_complete_question(text):
            continue
        question_match = re.match(r"\s*(\d{1,3})\s*[.)]?", text)
        if not question_match:
            # 문항 번호를 못 읽으면 AI가 기출 분석 소제목과 짝지을 수 없어
            # 정리본에 쓰이지 못하고 후보 목록만 불필요하게 늘린다.
            continue
        regions.append((question_match.group(1), rect, text, xref))

    # PowerPoint 원본에서 OX 문항의 A·B 진술이 서로 다른 이미지 조각으로
    # 저장되는 경우가 있다. 제목 조각부터 같은 열에서 바로 이어지는 조각을
    # 합쳐 페이지 영역으로 렌더링한다.
    for _, rect, text in image_parts:
        if not OX_QUESTION_PATTERN.search(text) or _looks_like_complete_question(text):
            continue
        related = [
            (part_rect.y0, part_rect, part_text)
            for _, part_rect, part_text in image_parts
            if abs(part_rect.x0 - rect.x0) <= 12
            and part_rect.y0 >= rect.y0
            and part_rect.y0 <= rect.y1 + 55
        ]
        related.sort()
        combined_rect = rect
        combined_text_parts = []
        last_bottom = rect.y0
        for _, part_rect, part_text in related:
            if part_rect.y0 > last_bottom + 14:
                break
            combined_rect |= part_rect
            combined_text_parts.append(part_text)
            last_bottom = max(last_bottom, part_rect.y1)
        combined_text = "\n".join(combined_text_parts)
        if _looks_like_complete_question(combined_text):
            question_match = re.match(r"\s*(\d{1,3})\s*[.)]?", combined_text)
            if question_match:
                regions.append(
                    (question_match.group(1), combined_rect, combined_text, None)
                )

    # 선택지가 많은 문항은 원본에서 상단(문항+앞쪽 선택지)과 하단(나머지
    # 선택지)이 서로 다른 이미지 조각으로 저장되기도 한다. 앞쪽 조각만도
    # `_looks_like_complete_question`을 통과할 만큼(질문 문구 + 선택지 2개
    # 이상) 완전해 보여서, 뒤쪽 선택지 조각이 그대로 버려져 정답 선택지가
    # 잘려나간 채 저장되는 문제가 있었다. 같은 열 바로 아래에 새 문항
    # 번호로 시작하지 않는 조각이 더 있으면 계속 이어 붙인다.
    def _is_new_question_start(text):
        return bool(re.match(r"\s*\d{1,3}\s*[.)]\s+\S", text or ""))

    extended_regions = []
    for question_no, rect, text, xref in regions:
        combined_rect = rect
        combined_text = text
        last_bottom = rect.y1
        used_rects = {id(rect)}
        while True:
            continuation = None
            for other_xref, other_rect, other_text in image_parts:
                if id(other_rect) in used_rects:
                    continue
                if abs(other_rect.x0 - rect.x0) > 12:
                    continue
                if other_rect.y0 < last_bottom - 2 or other_rect.y0 > last_bottom + 20:
                    continue
                if _is_new_question_start(other_text):
                    continue
                continuation = (other_rect, other_text)
                break
            if not continuation:
                break
            other_rect, other_text = continuation
            combined_rect |= other_rect
            combined_text = combined_text + "\n" + other_text
            last_bottom = other_rect.y1
            used_rects.add(id(other_rect))
        extended_regions.append((question_no, combined_rect, combined_text, xref))
    regions = extended_regions

    # PowerPoint에서 만든 PDF는 같은 문항 이미지를 배경/마스크와 함께
    # 여러 번 참조할 수 있다. 넓은 원본을 우선 선택하고, 이미 선택한 영역과
    # 대부분 겹치는 작은 중복만 제거한다.
    selected = []
    seen_xrefs = set()
    for item in sorted(regions, key=lambda value: value[1].get_area(), reverse=True):
        _, rect, _, xref = item
        if xref is not None and xref in seen_xrefs:
            continue
        is_duplicate = False
        for _, selected_rect, _, _ in selected:
            intersection = rect & selected_rect
            if (
                intersection.is_valid
                and intersection.get_area() >= min(rect.get_area(), selected_rect.get_area()) * 0.72
            ):
                is_duplicate = True
                break
        if is_duplicate:
            continue
        selected.append(item)
        if xref is not None:
            seen_xrefs.add(xref)

    # 학생이 여러 해의 기출 스크린샷을 보라색 박스로 감싸 한 슬라이드에
    # 모아 만든 자료는 페이지 전체가 통짜 이미지 하나로 저장돼, 위
    # 로직으로는 문항을 못 나누고 페이지 전체를 문항 하나로 잡는다.
    # 페이지 대부분을 차지하는 영역만 보라색 박스 탐지를 시도해, 실제로
    # 2개 이상 찾히면 그 문항들로 대체한다(못 찾으면 원래 영역 유지).
    page_area = page.rect.get_area()
    expanded = []
    for item in selected:
        question_no, rect, text, xref = item
        covers_most_of_page = (
            page_area > 0 and rect.get_area() / page_area >= FULL_PAGE_AREA_RATIO
        )
        sub_regions = (
            _split_purple_labeled_regions(page, rect, xref)
            if covers_most_of_page
            else []
        )
        expanded.extend(sub_regions if sub_regions else [item])

    expanded.sort(key=lambda value: (value[1].y0, value[1].x0))
    return expanded


def prepare_exam_figures(
    lecture_id,
    lecture_pdf_path,
    lecture_pages,
    exam_files,
    output_dir,
    max_lecture_pages=200,
    max_external_pages=48,
):
    """강의록 자체에 포함된 기출 문항만 이미지로 만들어 반환한다."""
    # 하위 호환을 위해 인자는 유지하지만 별도 기출 폴더 자료는 사용하지 않는다.
    del exam_files, max_external_pages
    output_dir = Path(output_dir)
    lecture_text = "\n".join(
        f'{page.get("text", "")}\n{page.get("ocr_text", "")}'
        for page in lecture_pages
    )
    lecture_terms = _tokens(lecture_text)
    candidates = []

    # 1순위: 강의록 안에 명시적으로 기출 표시가 있는 페이지.
    if lecture_pdf_path and Path(lecture_pdf_path).exists():
        with pymupdf.open(lecture_pdf_path) as document:
            for page_no, page in enumerate(document, start=1):
                row = lecture_pages[page_no - 1] if page_no <= len(lecture_pages) else {}
                text = f'{row.get("text", "")}\n{row.get("ocr_text", "")}'
                if not EXAM_PATTERN.search(text):
                    continue
                # 강의록 표지의 '19 중간 박용식/누락' 같은 출제 이력은
                # 문제 원문이 아니다. 실제 질문과 선택지가 잡힌 영역만 사용한다.
                # 한 페이지에 사진(그림) 문항과 실제 PDF 텍스트 문항이 섞여
                # 있을 수 있다(예: 두 문항은 이미지, 한 문항은 원문 텍스트).
                # 예전에는 이미지 문항이 하나라도 잡히면 텍스트 기반 탐색을
                # 아예 건너뛰어, 같은 페이지의 순수 텍스트 문항을 통째로
                # 놓쳤다. 이제는 이미지 영역과 크게 겹치지 않는 텍스트
                # 영역을 항상 함께 모은다.
                embedded_regions = _embedded_question_regions(page)
                regions = list(embedded_regions)
                text_regions = [
                    (*region, None) for region in _question_regions(page, embedded_regions)
                    if _looks_like_complete_question(region[2])
                ]
                for candidate in text_regions:
                    candidate_no, candidate_clip, _, _ = candidate
                    # 같은 문항 번호를 가진 그림 영역과 실제로 겹칠 때만
                    # 중복으로 본다(같은 문항이 이미지·텍스트 양쪽으로
                    # 잡힌 경우). 번호가 다르면, 옆·아래 칼럼의 그림
                    # 문항과 좌표가 겹치더라도 서로 다른 문항이므로
                    # 텍스트 영역을 버리지 않는다. 예전에는 넓이 비율만
                    # 보고 판단해, 같은 페이지에 있는 작은 그림 하나 때문에
                    # 훨씬 큰 진짜 문항 텍스트 전체가 사라지거나(번호 다름),
                    # 반대로 진짜 중복(번호 같음)을 못 걸러내는 문제가 있었다.
                    is_duplicate = any(
                        embedded_no == candidate_no
                        and (candidate_clip & embedded_rect).is_valid
                        and (candidate_clip & embedded_rect).get_area() > 0
                        for embedded_no, embedded_rect, _, _ in embedded_regions
                    )
                    if not is_duplicate:
                        regions.append(candidate)
                regions.sort(key=lambda region: (region[1].y0, region[1].x0))
                if not regions:
                    continue
                for region_index, (question_no, clip, region_text, _image_xref) in enumerate(regions):
                    question_suffix = f"-q{question_no}" if question_no else ""
                    # OCR이 같은 번호를 중복 인식해도 페이지 내 영역 순번으로
                    # 항상 고유한 키가 만들어지게 한다.
                    key = f"lecture-p{page_no}-r{region_index + 1}{question_suffix}"
                    image_path = output_dir / f"{key}.png"
                    # PDF 내부의 원본 이미지 객체에는 화면에서 가려진 정답
                    # 표시나 편집 전 레이어가 남아 있을 수 있다. 따라서 객체를
                    # 직접 꺼내지 않고, 사용자가 강의록에서 실제로 보는 최종
                    # 페이지 합성 상태를 문항 영역으로 렌더링한다.
                    _render_page(page, image_path, clip)
                    candidates.append({
                        "figure_key": key,
                        "source_kind": "lecture",
                        "source_label": (
                            f"강의록 {page_no}페이지"
                            + (f" · {question_no}번" if question_no else "")
                        ),
                        "source_path": str(lecture_pdf_path),
                        "source_order": page_no * 100 + region_index,
                        "page_no": page_no,
                        "question_no": question_no,
                        "image_path": str(image_path),
                        "extracted_text": region_text.strip(),
                        "relevance": 1000 + len(_tokens(region_text) & lecture_terms),
                    })
                if sum(item["source_kind"] == "lecture" for item in candidates) >= max_lecture_pages:
                    break

    result = candidates
    result.sort(key=lambda item: (item["source_order"], item["page_no"]))
    for item in result:
        item.pop("relevance", None)
        item.pop("_page_index", None)
        item.pop("_clip", None)
        item.pop("_shared_clip", None)
    return result
