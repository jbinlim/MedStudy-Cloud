import re
from difflib import SequenceMatcher

from quiz_utils import delatexify


IMAGE_PAGE_KEYWORDS = (
    "구조",
    "과정",
    "단면",
    "자세",
    "움직임",
    "발생",
    "기전",
    "경로",
    "분류",
    "그림",
    "도표",
    "structure",
    "process",
    "plane",
    "position",
    "movement",
    "development",
    "pathway",
    "cycle",
)


AGGREGATE_EXAM_SECTION = re.compile(
    r"(?ms)^###\s+[^\n]*나머지\s+직접\s+관련\s+문항[^\n]*\n"
    r".*?(?=^#{1,3}\s+|\Z)"
)

STRAY_EXAM_ANSWER_BULLET = re.compile(
    r"(?m)^[ \t]*[-*+]\s*\*{0,2}관련\s*기출(?:\s*문제)?\s*정답\*{0,2}\s*:[^\n]*\n?"
)

# 표의 구분 행(`|---|---|`)은 항상 `|`로 시작해 여기 안 걸린다. 소제목
# 사이에 낀 `---` 한 줄짜리 가로 구분선만 지운다(이 화면은 그 문법을
# 줄로 그려주지 않아 글자 그대로 보인다).
STANDALONE_HORIZONTAL_RULE = re.compile(r"(?m)^[ \t]*-{3,}[ \t]*$\n?")

MEMORY_LABELS = {
    "암기", "개수", "굽이", "급이", "형태", "주의", "혼동", "방향",
    "구조", "용어", "순서", "위치", "관계", "구별", "함정",
}


def normalize_memory_section(markdown_text):
    """암기 섹션의 라벨 글머리표를 소제목+목록 구조로 복원한다."""
    section = re.search(
        r"(?ms)^(##\s+5\.\s*암기 및 혼동 포인트\s*$)(.*?)(?=^##\s+6\.|\Z)",
        markdown_text,
    )
    if not section:
        return markdown_text

    lines = section.group(2).strip("\n").splitlines()
    output = []
    for line in lines:
        bullet = re.match(r"^\s*-\s+(.+)$", line)
        match = None
        if bullet:
            item = bullet.group(1).strip()
            item = re.sub(r"^\*\*|\*\*$", "", item).strip()
            item = re.sub(r"^(?:🧠|⚠️|⚠)\s*", "", item)
            item = re.sub(r"^\[암기\]\s*", "", item)
            topic_match = re.match(
                r"^(?:암기|주의)\s*[—–-]\s*(.+)$", item
            )
            if topic_match:
                if output and output[-1] != "":
                    output.append("")
                output.extend([
                    f"#### {topic_match.group(1).strip()}",
                    "",
                ])
                continue
            match = re.match(r"^([^:]{1,20}):\s*(.+)$", item)
        if match and match.group(1).strip() in MEMORY_LABELS:
            label, content = match.groups()
            if output and output[-1] != "":
                output.append("")
            output.extend([f"#### {label.strip()}", f"- {content.strip()}", ""])
        else:
            output.append(line)

    normalized_body = "\n".join(output).strip()
    # `**암기: 주제**`처럼 라벨과 주제가 한 줄에 붙은 경우도
    # 주제 자체를 소제목으로 승격해 같은 계층으로 보이게 한다.
    normalized_body = re.sub(
        r"(?m)^####\s+(암기|주의)\s*\n-\s+([^\n]+)$",
        r"#### \2",
        normalized_body,
    )
    replacement = section.group(1) + "\n\n" + normalized_body + "\n\n"
    return markdown_text[:section.start()] + replacement + markdown_text[section.end():]


EXAM_HEADING_WITH_FIGURE = re.compile(
    r"(### 🎯 \[\d{4}\s*(?:중간|기말)\s*)(\d+)"
    r"(\s*번\s*,\s*강의록[^\]]*\][^\n]*\n+\[\[EXAM_FIGURE:)"
    r"([^|\]]+)(\|)"
)


def fix_exam_heading_question_numbers(summary, exam_figures):
    """기출 소제목의 문항 번호를 원문 이미지의 실제 문항 번호로 맞춘다.

    같은 페이지에 선택지만 다르고 형태가 비슷한 문항이 여러 개 있으면,
    AI가 소제목에 같은 페이지의 다른 문항 번호를 잘못 적는 경우가 있다.
    소제목 바로 아래 `[[EXAM_FIGURE:...]]`가 가리키는 그림은 크롭 단계에서
    이미 정확한 문항 번호를 가지고 있으므로, 그 값으로 소제목의 번호를
    덮어써 항상 일치시킨다. `[YYYY 중간/기말 N번, 강의록 N페이지]`
    형식의 새 소제목에만 적용되며, 옛 형식은 건드리지 않는다.
    """
    question_no_by_key = {
        figure["figure_key"]: str(figure["question_no"]).strip()
        for figure in (exam_figures or [])
        if figure.get("question_no")
    }
    if not question_no_by_key:
        return summary

    def _replace(match):
        prefix, heading_no, middle, figure_key, pipe = match.groups()
        correct_no = question_no_by_key.get(figure_key.strip())
        if not correct_no or correct_no == heading_no:
            return match.group(0)
        return f"{prefix}{correct_no}{middle}{figure_key}{pipe}"

    return EXAM_HEADING_WITH_FIGURE.sub(_replace, summary)


EXAM_FIGURE_MARKER_TYPO = re.compile(r"\[\[\s*EXAM[_\s-]*FIGURE\s*:", re.IGNORECASE)
EXAM_HIGHLIGHT_MARKER_TYPO = re.compile(
    r"\[\[\s*EXAM[_\s-]*HIGHLIGHT\s*:", re.IGNORECASE
)


def normalize_exam_markers(summary):
    """`EXAM_FIGURE`/`EXAM_HIGHLIGHT` 표식의 언더스코어 누락·대소문자
    차이를 정규 형식으로 바로잡는다.

    Codex가 가끔 `[[EXAM_FIGURE:...]]`를 `[[EXAMFIGURE:...]]`처럼
    언더스코어 없이 쓴다. 문항 분석 내용 자체는 멀쩡한데, 렌더러와
    `find_missing_exam_figures`처럼 정확한 문자열만 찾는 다운스트림
    코드가 이를 인식하지 못해 기출 원문 이미지와 하이라이트가 정리본
    전체에서 통째로 안 보이게 된다.
    """
    normalized = EXAM_FIGURE_MARKER_TYPO.sub("[[EXAM_FIGURE:", summary or "")
    normalized = EXAM_HIGHLIGHT_MARKER_TYPO.sub("[[EXAM_HIGHLIGHT:", normalized)
    return normalized


def sanitize_generated_summary(summary, exam_figures=None):
    """학습 가치가 낮은 기출 묶음 요약을 최종 정리본에서 제거한다."""
    cleaned = normalize_exam_markers(summary or "")
    cleaned = AGGREGATE_EXAM_SECTION.sub("", cleaned)
    cleaned = STRAY_EXAM_ANSWER_BULLET.sub("", cleaned)
    cleaned = STANDALONE_HORIZONTAL_RULE.sub("", cleaned)
    cleaned = fix_exam_heading_question_numbers(cleaned, exam_figures)
    cleaned = normalize_memory_section(cleaned)
    cleaned = delatexify(cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


EXAM_SECTION_HEADING = re.compile(r"^##\s*기출문제\s*상세\s*분석\s*$", re.MULTILINE)
NEXT_TOP_LEVEL_SECTION = re.compile(r"^## 5\.", re.MULTILINE)


def split_summary_sections(summary):
    """정리본에서 문항별 상세 기출 분석(`## 기출문제 상세 분석`)만 떼어낸다.

    기출이 많은 강의는 문항별 상세 분석이 정리본 대부분을 차지해, 정작
    핵심 개념·강조 내용은 상대적으로 빈약해 보인다. 화면에서 "AI 정리본"과
    "기출 분석"을 별개 탭·PDF로 나눠 보여줄 수 있도록, 본문(1–7번, `## 4.
    자주 출제되는 내용`이라는 간략한 빈출 요약을 포함)과 문항별 상세 분석을
    분리해 반환한다. 상세 분석이 없으면 두 번째 값은 빈 문자열이다.
    """
    summary = summary or ""
    match = EXAM_SECTION_HEADING.search(summary)
    if not match:
        return summary, ""

    next_match = NEXT_TOP_LEVEL_SECTION.search(summary, match.end())
    exam_end = next_match.start() if next_match else len(summary)
    exam_section = summary[match.start():exam_end].strip()
    main_summary = (summary[:match.start()] + summary[exam_end:]).strip()
    main_summary = re.sub(r"\n{3,}", "\n\n", main_summary)
    return main_summary, exam_section


def find_missing_exam_figures(summary, exam_figures):
    """`## 4. 기출문제 분석`에서 언급되지 않은 기출 원문 이미지를 찾는다."""
    referenced_keys = set(
        re.findall(r"\[\[EXAM_FIGURE:([^|\]]+)\|", summary or "")
    )
    return [
        figure for figure in (exam_figures or [])
        if figure["figure_key"] not in referenced_keys
    ]


def build_exam_analysis_request(subject_name, lecture_title, figures):
    """`## 4. 기출문제 분석`에 실릴 문항 분석을 요청한다.

    정리본 본문(build_chatgpt_request)과 별개 Codex 호출로 기출 분석만
    맡긴다. 기출이 많은 강의는 본문과 기출 분석을 한 번에 요청하면 AI가
    분량을 나눠 쓰면서 본문 설명이 압축되는 문제가 있어, 완전히 분리된
    요청으로 기출 분석에만 집중하게 한다. 문항을 몇 개씩 나눠 보낼 때
    (배치별 진행)와 정리본 작성 후 빠진 문항을 보충할 때 모두 같은
    형식으로 재사용한다.
    """
    figure_list = "\n\n".join(
        f'- 그림키: {figure["figure_key"]}\n'
        f'  출처: {figure.get("source_label", "")}\n'
        f'  OCR 원문: {(figure.get("extracted_text") or "").strip()[:500]}'
        for figure in figures
    )
    return f"""
과목: {subject_name}
강의: {lecture_title}

아래는 이 강의 강의록 안에서 실제로 확인된 기출 문항들입니다. 하나도
빠짐없이 분석에 포함해야 합니다. 각 문항마다 문제 자체가 크롭된 이미지와,
그 문항이 있는 강의록 원본 페이지 이미지를 함께 첨부했습니다. 목록에 없는
문항을 새로 만들지 마세요.

소제목은 반드시 `### 🎯 [YYYY 중간(또는 기말) N번, 강의록 N페이지] 문제 주제`
형식 그대로 쓰세요 (예: `### 🎯 [2025 중간 3번, 강의록 13페이지] 월경기에
관찰되는 자궁내막층`). 연도·중간/기말·문항 번호는 크롭 이미지가 아니라
강의록 원본 페이지에서 문항 상자 위에 적힌 `25 중간 객3` 같은 출처 표시를
그대로 가져오세요. 두 자리 연도는 `20YY` 네 자리로, 문항 번호는 `객3`이
아니라 `3번`으로 바꿔 쓰세요. 강의록 표지의 출제 횟수·담당 교수·누락 여부
목록은 문항이 아니므로 분석 대상에서 완전히 무시하세요.

### 🎯 [YYYY 중간(또는 기말) N번, 강의록 N페이지] 문제 주제

[[EXAM_FIGURE:그림키|원본 문제]]

> **문제의 요점:** 이 문제가 수험생에게 무엇을 구별하거나 판단하라고 묻는지 한 문장으로 설명

- **필요한 핵심 개념:** 문제를 풀기 전에 알아야 하는 배경 설명
- **정답 및 판단 근거:** 정답과 그 이유. `확정`, `교수 언급`, `추정`, `판단 불가` 중
  하나로 확실도를 표시
- **선지 판단:** 확인 가능한 범위에서 주요 오답이 왜 틀렸는지 설명
- **자주 빠지는 함정:** 비슷한 용어, 인물, 방향 또는 예외를 구분
- **시험장에서 기억할 한 줄:** 실제 풀이에 바로 쓸 수 있는 판별 기준
- **근거:** 관련 PDF 페이지 또는 전사문

주의할 점:
- 문제 문장이나 그림이 잘 안 읽혀도 문항 자체를 건너뛰지 말고, 확인되는
  선택지에서 판별 가능한 내용을 설명한 뒤 무엇이 불확실한지 명시하세요.
  자료에 없는 사실은 만들어내지 마세요.
- 정답표가 여러 정답을 제시하면(`①·④`, `복수 정답` 등) 하나만 고르지 말고
  모든 정답 번호를 그대로 보존하세요.
- **한 그림 안에 같은 문제가 여러 해·여러 시험에서 반복 출제된 스크린샷이
  함께 모여 있으면**(학생이 만든 자료라 25 중간·25 기말·23 중간·23 기말처럼
  서로 다른 회차를 한 슬라이드에 모아 넣은 경우가 있습니다), 그 중 하나만
  골라 소제목을 달지 마세요. 소제목에 확인되는 모든 회차를 가운뎃점(·)으로
  나열하세요. 예: `### 🎯 [반복 출제 25 중간 17번·25 기말 21번·23 중간
  21번·23 기말 21번, 강의록 26페이지] 교근(masseter) 식별`. 본문에도 회차별
  정답과 판단 근거를 각각 구분해서 모두 적으세요.
- **같은 그림을 재사용하며 표시 위치(A·B·C)나 선택지 순서만 바꾼 유사
  문항이 여러 개 있으면, 선택지 번호와 내용의 대응은 반드시 지금 분석하는
  그 문항 자신의 이미지에서 직접 읽으세요.** 다른 문항의 선택지 순서를
  기억해 옮기면 번호는 맞아도 내용이 바뀌는 실수가 생깁니다.
- 공통 제시문·도판을 여러 문항이 함께 쓰면, 그 도판과 해당 개별 문항이 함께
  담긴 이미지를 사용하세요.
- A·B 문장을 각각 O/X로 판단하는 문항도 독립된 기출 문항이니 빠뜨리지 말고
  각 진술의 O/X와 이유를 따로 설명하세요.
- 정답표·답지 페이지 자체는 이미지로 넣지 말고, 정답과 판단 근거는 텍스트로만
  쓰세요. 여러 문항을 "나머지 관련 문항"처럼 한 섹션에 묶어 번호만 나열하지
  마세요.
- 서론·설명·코드 블록 없이 각 문항을 위 형식으로만 출력하세요.

기출 문항 목록:
{figure_list}
""".strip()


def insert_missing_exam_sections(summary, addition):
    """기출문제 분석 끝(반복 출제 패턴 앞)에 보충한 문항 분석을 끼워 넣는다."""
    addition = (addition or "").strip()
    if not addition:
        return summary

    pattern_heading = re.search(
        r"(?m)^###\s+기출에서\s+반복된\s+출제\s+패턴", summary
    )
    if pattern_heading:
        insert_at = pattern_heading.start()
    else:
        section5 = re.search(r"(?m)^##\s+5\.", summary)
        insert_at = section5.start() if section5 else len(summary)

    return (
        summary[:insert_at].rstrip("\n")
        + "\n\n" + addition + "\n\n"
        + summary[insert_at:]
    ).strip() + "\n"


def insert_frequent_topics_section(summary, addition):
    """`## 4. 자주 출제되는 내용` 간략 요약을 상세 분석 바로 앞에 끼워 넣는다.

    문항별 상세 분석(`## 기출문제 상세 분석`)은 정리본 본문에서 빠지고
    별도 탭·PDF로 옮겼지만, 그러면 정리본에 "4번" 자체가 통째로 사라져
    반복 출제 개념을 한눈에 보기 어렵다. 상세 분석 바로 앞에 간략한
    빈출 요약을 꽂아 두면, `split_summary_sections`가 본문을 뗄 때
    이 요약은 본문에 남고 상세 분석만 별도로 분리된다.
    """
    addition = (addition or "").strip()
    if not addition:
        return summary

    detail_heading = EXAM_SECTION_HEADING.search(summary)
    if detail_heading:
        insert_at = detail_heading.start()
    else:
        section5 = re.search(r"(?m)^##\s+5\.", summary)
        insert_at = section5.start() if section5 else len(summary)

    return (
        summary[:insert_at].rstrip("\n")
        + "\n\n" + addition + "\n\n"
        + summary[insert_at:]
    ).strip() + "\n"


def build_frequent_exam_topics_request(subject_name, lecture_title, exam_analysis_content):
    """정리본 `## 4.`에 넣을 간략한 빈출 개념 요약을 요청한다.

    문항별 상세 분석(각 문항의 정답 근거·함정·판별 기준)을 다시 읽어,
    문항 번호나 개별 정답을 반복하지 말고 여러 문항에 걸쳐 반복되는
    개념·판별 기준만 간추린다. 정리본 본문에서 "이 강의는 무엇이 자주
    나오는가"를 한눈에 보여주는 용도이며, 자세한 문항별 근거는 별도
    "기출 분석" 탭에 그대로 남아 있다.
    """
    return f"""
과목: {subject_name}
강의: {lecture_title}

아래는 이 강의 기출 문항들을 하나하나 분석한 내용입니다. 문항 번호나
개별 정답을 다시 나열하지 말고, 여러 문항에 걸쳐 반복되는 개념·헷갈리는
포인트·판별 기준만 간추려 정리본의 "자주 출제되는 내용" 섹션으로
요약하세요.

반드시 다음 형식을 그대로 지키세요.

## 4. 자주 출제되는 내용

- 반복되는 주제마다 `**주제명:**`으로 시작해, 무엇을 헷갈리는지와 구별
  기준을 1-2문장으로 설명하세요.
- 실제 여러 문항에서 확인되는 패턴만 쓰고, 새로운 사실을 만들어내지
  마세요.
- 개별 문항 번호, 연도, 정답 선지 번호는 언급하지 마세요(상세 내용은
  별도 "기출 분석"에 있습니다).
- 5-10개 항목으로 간결하게 작성하세요.
- 화학·물리 계산이 있어도 LaTeX 문법(`\\(`, `\\)`, `\\times`, `\\mathrm{{}}`,
  `^{{}}`, `_{{}}` 등)을 쓰지 마세요. 대신 일반 텍스트와 유니코드 기호로
  쓰세요(예: `4.0×10⁻⁴ mol/L`, `[OH⁻]`, `pKₐ`).

서론·설명·코드 블록 없이 "## 4. 자주 출제되는 내용" 섹션만 출력하세요.

문항별 상세 분석:
{exam_analysis_content}
""".strip()


def suggest_summary_image_pages(
    lecture_pages,
    max_pages=8,
):
    scored_pages = []

    for page in lecture_pages:
        image_path = page["image_path"]

        if not image_path:
            continue

        searchable_text = (
            f'{page["text"]}\n{page["ocr_text"]}'
        ).lower()
        score = sum(
            searchable_text.count(keyword.lower())
            for keyword in IMAGE_PAGE_KEYWORDS
        )
        text_length = len(searchable_text.strip())

        if 30 <= text_length <= 900:
            score += 2

        if text_length < 30:
            score -= 2

        scored_pages.append(
            (
                score,
                page["page_no"],
            )
        )

    selected = sorted(
        scored_pages,
        key=lambda item: (-item[0], item[1]),
    )[:max_pages]

    return sorted(
        page_no
        for _, page_no in selected
    )


def build_chatgpt_request(
    subject_name,
    lecture_no,
    lecture_title,
    lecture_pages,
    transcript_text,
    selected_image_pages=None,
    figures=None,
):
    """ChatGPT에 직접 업로드할 Markdown 요청 파일을 만든다.

    본문(1·2·3·5·6·7번)만 요청하고 `## 4. 기출문제 분석`은 만들지 않는다.
    기출이 많은 강의에서 본문과 기출 분석을 한 요청에 같이 시키면 AI가
    답변 분량을 나눠 쓰면서 본문 설명이 얕아지는 문제가 있어, 기출 분석은
    `build_exam_analysis_request`로 완전히 분리된 요청을 따로 보낸다.
    같은 이유로 EXAM_HIGHLIGHT 표시도 이 호출에서는 시키지 않는다(기출
    내용을 못 보므로 잘못 짚을 위험이 있다) — 두 결과를 합친 뒤
    `exam_highlight_service.highlight_summary_with_codex`가 정확한
    근거로 별도 표시한다.
    """
    page_sections = []

    for page in lecture_pages:
        page_text = page["text"].strip()
        ocr_text = page["ocr_text"].strip()

        if page_text or ocr_text:
            page_parts = []

            if page_text:
                page_parts.append(
                    "**PDF 원문 텍스트**\n\n"
                    f"{page_text}"
                )

            normalized_page = " ".join(page_text.split())
            normalized_ocr = " ".join(ocr_text.split())
            texts_are_similar = bool(
                normalized_page
                and normalized_ocr
                and (
                    normalized_page in normalized_ocr
                    or normalized_ocr in normalized_page
                    or SequenceMatcher(
                        None,
                        normalized_page,
                        normalized_ocr,
                        autojunk=False,
                    ).ratio() >= 0.86
                )
            )

            if ocr_text and not texts_are_similar:
                page_parts.append(
                    "**페이지 이미지 OCR 보완 텍스트**\n\n"
                    f"{ocr_text}"
                )

            page_sections.append(
                f'### PDF {page["page_no"]}페이지\n\n'
                + "\n\n".join(page_parts)
            )
        else:
            page_sections.append(
                f'### PDF {page["page_no"]}페이지\n\n'
                "(추출된 텍스트 없음 - 원본 PDF의 "
                "이미지와 도표를 직접 확인할 것)"
            )

    title = (
        f"{lecture_no}강 {lecture_title}"
        if lecture_title
        else f"{lecture_no}강"
    )

    selected_image_pages = (
        selected_image_pages or []
    )
    selected_image_text = (
        ", ".join(
            f"{page_no}페이지"
            for page_no in selected_image_pages
        )
        or "없음"
    )
    figures = figures or []
    figure_text = (
        "\n".join(
            (
                f'- {figure["figure_key"]}: '
                f'PDF {figure["page_no"]}페이지, '
                f'{figure["caption"]}'
            )
            for figure in figures
        )
        or "- 추출된 핵심 그림 없음"
    )

    return f"""# MedStudy 강의 정리 요청

## 강의 정보

- 과목: {subject_name}
- 강의: {title}

## ChatGPT 작업 지시

이 파일과 원본 강의록 PDF를 함께 참고하여, 이 정리본만 읽어도 강의를 이해하고
시험을 준비할 수 있는 독립적인 의대 학습 정리본을 작성해 주세요.

### 작성 원칙

- 자료에 실제로 있는 내용만 사용하고 없는 사실을 만들어내지 마세요.
- 긴 자료는 구간별로 먼저 분석한 뒤 중복을 제거하여 통합하세요.
- 단순히 원문을 나열하지 말고 개념 사이의 관계와 이유를 설명하세요.
- 용어와 약어는 처음 등장할 때 뜻을 풀어 쓰세요.
- 표는 비교가 명확해질 때만 사용하고, 긴 문장을 표 안에 억지로 넣지 마세요.
- 표를 쓸 때는 이 화면이 표를 그리드가 아니라 "각 열 머리글: 그 칸 값"
  형식의 줄로 풀어서 보여줍니다. 그래서 머리글 행의 각 칸에는 반드시 그
  아래 값이 무엇을 뜻하는지 알 수 있는 제목을 채우세요(예: 첫 열이
  비교 대상 이름이라면 머리글도 `이름`처럼 채우고, 절대 빈 칸으로
  두지 마세요). 머리글 칸을 비워두면 그 줄이 `: 값`처럼 라벨 없이
  깨져 보입니다.
- 중요한 그림이나 도표는 반드시 원본 PDF 페이지 번호를 표시하세요.
- 첨부된 중요 페이지 이미지를 직접 확인하고 글자뿐 아니라 구조, 화살표, 위치 관계와
  도표의 의미를 설명하세요.
- 문제 원문이나 그림이 없어 확정할 수 없는 내용은 `확인 필요` 또는 `추정`으로 표시하세요.
- 근거 표시는 모든 문장에 반복하지 말고, 교수 강조·기출 분석·자료 간 충돌·불확실한
  주장에만 붙이세요. 형식은 `[근거: PDF n페이지]`, `[근거: 전사문]`,
  `[근거: PDF n페이지 + 전사문]` 중 하나를 사용하세요.
- 답이나 해석의 확실도는 필요한 경우에만 `확정`, `교수 언급`, `추정`, `판단 불가`로
  구분하세요. 자료로 확인할 수 없는 답을 `확정`으로 쓰지 마세요.
- 정답표·강의록에 같은 문항의 정답이 2개 이상(예: `①·④`, `1, 4번`,
  `복수 정답`)으로 명시되어 있으면 반드시 **모든 정답을 함께** 표시하세요.
  임의로 첫 번째 번호만 남기거나 단일 정답으로 바꾸지 마세요. 정답 번호와
  선지 내용이 확인되면 `① distal, ④ proximal — 확정`처럼 작성하고,
  복수 정답 여부도 명시하세요.
- Markdown 코드 블록은 사용하지 마세요.
- 가로 구분선(`---`)을 소제목 사이에 넣지 마세요. 이 화면은 그 문법을
  줄로 그려주지 않아 글자 그대로 `---`가 보입니다. `##`, `###` 소제목
  만으로 구획을 충분히 나눌 수 있습니다.
- 수정 전후를 표시하는 취소선(`~~내용~~`, `<del>`, `<s>`)은 절대 사용하지 마세요.
- 잘못된 표현을 함께 남기지 말고 확인된 최종 내용만 자연스러운 문장으로 작성하세요.
- 숫자 범위는 `1~3주`처럼 물결표를 쓰지 말고 `1–3주`처럼 en dash를 사용하세요.
- 화학·물리 계산이 있어도 LaTeX 문법(`\\(`, `\\)`, `\\times`, `\\mathrm{{}}`,
  `^{{}}`, `_{{}}` 등)을 쓰지 마세요. 이 화면은 LaTeX를 수식으로 그려주지 않아
  글자가 깨진 채로 그대로 보입니다. 대신 일반 텍스트와 유니코드 기호로 쓰세요
  (예: `4.0×10⁻⁴ mol/L`, `[OH⁻]`, `pKₐ`, `pH = 14 - 3.4`).

### 이미지 삽입 규칙

- 그림 탐지 후보로 확인한 원본 PDF 페이지: {selected_image_text}
- 정리본에 첨부된 크롭 그림 목록:
{figure_text}
- 첨부된 크롭 그림 중 설명에 실제로 도움이 되는 것만 관련 개념 바로 아래에 삽입하세요.
- 삽입 위치에는 반드시 다음 형식을 한 줄로 정확히 출력하세요.
  `[[FIGURE:그림키|그림이 보여주는 핵심 내용을 설명하는 캡션]]`
- 이미지 표시는 글머리표나 표 안에 넣지 마세요.
- 같은 이미지를 두 번 넣지 마세요.
- 목록에 없는 그림키는 만들지 마세요.

### 고정 출력 양식

# {subject_name} - {title}

> **한눈에 보는 강의:** 강의 전체를 3-5문장으로 설명

## 1. 학습 목표

- 이 강의가 끝나면 설명할 수 있어야 하는 내용을 3-6개 제시

## 2. 핵심 개념

- 개념마다 가능한 범위에서 `정의 → 구조·기전 → 왜 중요한가 → 결과 → 임상 또는 시험 연결`
  순으로 설명하되, 자료에 없는 단계를 억지로 채우지 마세요.
- 서로 헷갈리는 개념은 비교표 사용
- 중요한 그림은 `(PDF n페이지 참고)` 표시
- 강의록 페이지에 기출문제나 정답이 실려 있어도 `관련 기출 정답`처럼 정답·선지
  번호를 이 섹션에 끌어오지 마세요. 기출 문항 분석은 완전히 별도 요청으로
  다뤄 별도 탭에 정리되므로, 여기서는 개념 설명 자체에만 집중하세요.

## 3. 교수님 강조 및 보충 설명

- `### ⭐ 교수 강조` 소제목 아래에 강조 내용을 간결한 목록으로 묶으세요.
- `### 💬 강의록에 없던 전사문 보충` 소제목 아래에 전사문 보충 내용을 묶으세요.
- 각 목록 항목마다 `교수 강조:`나 `전사문 보충:` 라벨을 반복하지 마세요.
- 교수 강조는 교수님이 중요·시험·암기를 직접 언급했거나, 같은 내용을 반복했거나,
  다른 내용보다 길고 구체적으로 설명한 경우에만 선정하세요.
- 단순히 전사문에 등장했다는 이유만으로 교수 강조로 분류하지 마세요.
- 각 강조 항목 끝에는 짧은 근거 표시를 붙이세요.

## 5. 암기 및 혼동 포인트

- 각 묶음의 제목은 반드시 `### 🧠 암기: 주제명` 또는 `### ⚠️ 주의: 주제명` 형식으로 작성하세요.
- 각 제목 아래에는 그 주제에 속하는 세부 내용만 `- 내용` 형식의 목록으로 작성하세요.
- 서로 다른 암기 주제를 하나의 평평한 목록에 섞지 마세요.
- 단순 단어 나열보다 서로 비교되는 개념, 순서, 예외와 판별 기준을 한 묶음으로 정리하세요.

## 6. 시험 직전 체크리스트

- 강의의 핵심 구별 기준·순서·예외·교수 강조를 실제로 회상하게 하는 짧은 주관식 또는 단답형 질문 5-10개
- 각 질문 바로 다음 줄에 `**정답:** 내용` 형식의 짧은 정답 한 줄을 표시하세요.
- 질문 문장과 `정답:`을 절대 같은 줄에 쓰지 마세요.
- 객관식 선택지는 넣지 마세요. 객관식 문제는 웹 복습 기능에서 별도로 생성합니다.
- 단순 정의 복사나 지나치게 쉬운 질문은 피하고, 틀리기 쉬운 내용을 우선하세요.

## 7. 자료 충돌·누락·확인 필요

- PDF와 전사문이 서로 다르게 말하는 내용, OCR 또는 전사 오류로 의심되는 용어,
  추출 실패, 문제 원문 누락, 추정 답안, 그림 확인이 필요한 페이지를 모아서 표시
- 항목마다 무엇이 부족한지와 확인할 원본 위치를 짧게 적으세요.
- 문제가 없으면 `특이사항 없음` 한 줄만 적으세요.

---

## PDF 페이지별 추출 텍스트

{chr(10).join(page_sections)}

---

## 교수님 전사문

{transcript_text.strip()}
"""
