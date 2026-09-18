import re

from codex_service import generate_summary_with_codex
from quiz_utils import delatexify, extract_review_quiz


HANGUL_PATTERN = re.compile(r"[가-힣]")
LATIN_WORD_PATTERN = re.compile(r"[A-Za-z]{2,}")

EXAM_STYLE_SAMPLE_LIMIT = 15
EXAM_STYLE_SAMPLE_CHARS = 220
EXAM_STYLE_SAMPLE_BUDGET = 3500


def _is_mostly_english(text):
    hangul_count = len(HANGUL_PATTERN.findall(text))
    latin_word_count = len(LATIN_WORD_PATTERN.findall(text))
    if hangul_count + latin_word_count < 8:
        return False
    return latin_word_count > hangul_count * 3


def build_exam_style_instruction(exam_texts):
    """정리본이 아니라 실제 기출 문항 원문에서 언어 스타일을 뽑아 그대로
    따르게 한다.

    정리본은 항상 한국어로 쓰이므로(ai_service의 고정 지시), 원래 시험이
    영어로 출제됐는지, 혹은 한국어 문장 안에 특정 의학 용어·고유명사만
    영어로 쓰였는지는 정리본으로는 알 수 없다. 강의록에서 크롭한 실제
    기출 문항 텍스트(`lecture_exam_figures.extracted_text`)나 과목
    기출 PDF 원문처럼 정리 이전 원본을 받아, 그 언어 패턴을 새 문항에도
    그대로 반영하도록 지시한다. 시험 전체가 영어면 전부 영어로, 한국어
    문장에 일부 용어만 영어로 쓰여 있다면 그 용어만 영어로 유지하도록
    구분해서 지시해, 있지도 않은 용어까지 억지로 영어로 바꾸는 것을
    막는다.
    """
    samples = [text.strip() for text in exam_texts if text and text.strip()]
    samples = samples[:EXAM_STYLE_SAMPLE_LIMIT]
    if not samples:
        return ""

    combined = " ".join(samples)
    hangul_count = len(HANGUL_PATTERN.findall(combined))
    latin_word_count = len(LATIN_WORD_PATTERN.findall(combined))
    # 기출에 영어 단어가 하나도 없으면 완전히 한국어인 기출이므로, 기존
    # 기본 동작(순수 한국어) 그대로 두고 아무 지시도 추가하지 않는다.
    if latin_word_count == 0:
        return ""

    # 문항마다 잘라 붙이되, 전체 길이가 너무 길어지지 않도록 예산 안에서
    # 담을 수 있는 만큼만 싣는다(그래도 기존보다 훨씬 많은 문항을 보여줘
    # 실제 영어 표기 용어를 폭넓게 확인할 수 있게 한다).
    included = []
    used_chars = 0
    for sample in samples:
        snippet = sample[:EXAM_STYLE_SAMPLE_CHARS]
        if used_chars + len(snippet) > EXAM_STYLE_SAMPLE_BUDGET and included:
            break
        included.append(snippet)
        used_chars += len(snippet)
    sample_block = "\n\n".join(f"- {snippet}" for snippet in included)

    if _is_mostly_english(combined):
        guidance = (
            "이 과목의 실제 시험은 영어로 출제됩니다. 질문, 선택지, 정답 "
            "표시, 해설을 포함한 모든 문항 내용을 영어로 작성하세요. 의학 "
            "용어는 실제 영어 시험에서 쓰는 표준 영어 용어를 사용하세요."
        )
    else:
        guidance = (
            "문장 구조는 한국어로 쓰되, 아래 실제 기출들을 보면 해부학적 "
            "구조명 등 전문 용어·고유명사를 한국어로 번역하지 않고 영어 "
            "그대로 표기하는 것이 이 과목의 표기 관례임을 알 수 있습니다. "
            "새 문항에서 언급하는 관련 전문 용어는, 아래 예시에 똑같은 "
            "단어가 나오지 않더라도 이 관례를 그대로 적용해 한국어로 "
            "번역하지 말고 영어로 쓰세요. 다만 이 관례와 무관한 일반 "
            "한국어 서술(질문 문장, 조사, 설명 등)까지 영어로 바꾸지는 "
            "마세요."
        )

    return f"""
언어 지시:
- {guidance}
- 아래 원문은 언어·표기 스타일 참고용입니다. 문항 소재로 그대로 재사용하지
  마세요.

실제 기출 문항 원문(언어 참고용):
{sample_block}
""".strip()


def detect_quiz_language(exam_texts):
    """실제 기출 문항 원문을 보고 최종 출력 언어를 "en"/"ko"로 정한다.

    `codex_service.generate_summary_with_codex`의 `output_language`에
    바로 넘겨, 시험 전체가 영어인 과목만 전체 출력을 영어로 강제한다.
    한국어 문장에 영어 용어가 섞인 경우는 `build_exam_style_instruction`의
    지시만으로 충분하므로 여기서는 "ko"로 남겨 둔다.
    """
    combined = " ".join(text for text in exam_texts if text)
    return "en" if _is_mostly_english(combined) else "ko"


QUIZ_QUALITY_RULES = """
출제자 역할:
- 실제 의대 시험을 출제하는 교수라고 생각하고, 강의의 학습목표와 반복 기출 개념을
  평가하는 모의고사 문항을 작성하세요.
- 학생이 정리본이나 원본 PDF를 옆에 두지 않아도 질문 자체만으로 무엇을 묻는지
  이해할 수 있는 완결된 문항이어야 합니다.
- 화학·물리 계산이 필요한 문항이라도 LaTeX 문법(`\\(`, `\\)`, `\\times`,
  `\\mathrm{}`, `^{}`, `_{}` 등)을 쓰지 마세요. 이 화면은 LaTeX를 수식으로
  그려주지 않아 글자가 깨진 채로 그대로 보입니다. 대신 일반 텍스트와
  유니코드 기호로 쓰세요(예: `4.0×10⁻⁴ mol/L`, `[OH⁻]`, `pKₐ`, `pH = 14 - 3.4`).

실제 기출을 난이도·문체의 기준으로 삼기:
- 자료에 포함된 실제 기출 문항(정리본의 `## 4. 기출문제 분석`, 첨부된 기출 원문)을
  먼저 살펴보고, 그 문항들의 **질문 길이, 문장 구조, 요구하는 사고 수준, 다루는
  세부 정도**를 새 문항의 기준으로 삼으세요. 실제 기출보다 뚜렷하게 쉽거나
  추상적인 문항을 만들지 마세요.
- 실제 기출이 임상 상황 제시형이면 새 문항도 비슷한 길이의 상황을 제시하고,
  실제 기출이 그림 판독·구조 식별형이면 새 문항도 구체적 구조·수치·조건을
  명시해 같은 판별 수준을 요구하세요. 기출과 완전히 동떨어진 형식으로 임의로
  단순화하지 마세요.
- 기출을 참고했다는 사실 자체를 질문이나 해설 문장에 언급하지 말고, 검증하려는
  개념만 자연스러운 문장으로 제시하세요(기존 규칙과 동일하게 출처 언급 금지).

출제 설계표(문항 번호 기준):
- 1–2번: 핵심 원리·기전을 이해해야 풀 수 있는 문제
- 3–4번: 서로 혼동하기 쉬운 개념·용어·수치·방향을 구별하는 문제
- 5–6번: 인과관계, 단계, 순서 또는 조건 변화에 따른 결과를 판단하는 문제
- 7–8번: 짧은 임상·실험·자료 해석 상황에 강의 개념을 적용하는 문제
- 9–10번: 기출의 반복 개념·함정·예외를 변형한 시험 대비 문제

문항 품질 기준:
- 단순히 문장을 그대로 복사하거나 용어 뜻만 묻는 문제는 최대 2개로 제한
- 한 문제에는 하나의 판단만 묻고, 가장 적절한 정답이 반드시 하나만 존재하게 작성
- 부정형·이중부정, `모두 옳다`, `정답 없음`, 말장난, 지나치게 사소한 숫자 암기는 피하기
- 불가피하게 부정형을 쓰면 질문에서 `옳지 않은 것`을 굵게 표시
- 네 선택지는 같은 범주와 문법 형태로 맞추고 길이만으로 정답이 드러나지 않게 작성
- 오답은 무관한 단어가 아니라 학습자가 실제로 혼동할 만한 인접 개념으로 작성
- 자료에 확정 근거가 없는 내용이나 강의 범위 밖의 의학 지식을 정답 근거로 사용하지 않기
- 해설은 `정답 근거 → 가장 매력적인 오답이 틀린 이유 → 시험장에서의 판별 기준` 순서로 2–4문장 작성
- `원문 누락`, `선택지 누락`, `OCR 오류`, `전사 오류`, `PDF 페이지`, `정리본 작성 방식`,
  `어떻게 기록해야 하는가`, `확정할 수 없는 답을 어떻게 처리하는가`처럼 자료 관리나
  정리 과정 자체를 묻는 문제는 절대 출제하지 않기
- `교수님 설명에 따르면`, `제공된 자료에 따르면`, `정리본에 따르면`처럼 출처를
  암기하게 하지 말고, 검증하려는 의학·과학 개념을 질문에 직접 제시하기
- 강의에서 언급됐다는 이유만으로 논문 개수, 페이지 번호, 파일 상태, 통계 집계 범위처럼
  학습목표와 무관한 주변 정보를 문제로 만들지 않기
- 불완전한 기출 문항은 문제 소재로 사용하지 말고, 그 기출이 가리키는 핵심 개념이
  강의 자료에서 충분히 확인될 때만 완결된 새 문항으로 재구성하기
- 전체 문항 중 최소 6개는 단순 회상이 아닌 감별, 인과, 순서, 임상·실험 상황 적용 중
  하나를 평가해야 합니다.

10문항을 다 쓴 뒤 스스로 검토하기:
- 각 문항의 오답 3개를 다시 읽고, "강의를 절반만 이해한 학생이라면 이 오답도
  정답처럼 보일까?"를 자문하세요. 오답이 누가 봐도 명백히 틀렸다면, 실제로
  헷갈릴 만한 인접 개념·유사 수치·반대 방향으로 다시 쓰세요.
- 전체 10문항의 체감 난이도가 자료에 있는 실제 기출보다 낮다고 판단되면,
  쉬운 문항 일부를 위 "실제 기출을 기준으로 삼기" 지침에 맞춰 다시 작성한
  뒤 최종본만 출력하세요. 검토 과정이나 초안은 출력하지 마세요.
""".strip()


QUIZ_QUALITY_EXAMPLE = """
형식·품질 기준을 모두 만족하는 예시 문항입니다 (형식만 참고하고 아래
소재를 실제 문항으로 재사용하지 마세요).

3. 안정 시 세포외액과 세포내액의 전기적 구획을 나누는 데 가장 크게
기여하는 것은?
A. 세포외액의 Na+과 세포내액의 K+ 농도차
B. 세포외액과 세포내액의 포도당 농도차
C. 세포외액과 세포내액의 총단백질량 차이
D. 세포외액과 세포내액의 pH 차이
**정답:** A
**해설:** Na+/K+ ATPase가 Na+을 세포 밖으로, K+을 세포 안으로 능동수송해
두 구획의 주요 양이온 농도차를 만들고, 이 농도차가 안정막전위의 기반이
된다. B는 포도당 농도차가 실제 구획 구분의 주된 근거가 아니라는 점에서
매력적인 오답이다. 시험장에서는 "구획을 나누는 전기적 기준"을 물으면
Na+/K+ 농도차부터 떠올린다.
""".strip()


FORBIDDEN_META_PATTERNS = (
    "원문 누락",
    "질문과 선택지가 누락",
    "선택지가 누락",
    "정리 방식",
    "정리본에 따르면",
    "제공된 자료에 따르면",
    "교수님 설명에 따르면",
    "교수님",
    "기출의",
    "기출 정답",
    "강의에 근거",
    "강의 그림",
    "그래프를 보고",
    "사진을 보고",
    "그림을 보고",
    "pdf 페이지",
    "ocr 오류",
    "전사 오류",
    "정답 번호는 기록",
    "미확정으로 둔다",
)


def _contains_meta_question(item):
    searchable = " ".join(
        [item.get("question", ""), *item.get("choices", {}).values()]
    ).lower()
    if any(pattern in searchable for pattern in FORBIDDEN_META_PATTERNS):
        return True
    return bool(
        re.search(
            r"(?:강의|정리본|교수님|기출).{0,10}(?:따르면|근거|설명)",
            searchable,
        )
    )


def _quiz_problems(items):
    """검증 실패 이유를 구체적으로 모아 재시도 프롬프트에 그대로 알려준다.

    이유를 밝히지 않고 통째로 다시 쓰게 하면 같은 실수가 반복되기 쉽다.
    무엇이 왜 잘못됐는지 짚어주면 한 번의 재시도로 통과할 확률이 높아져
    재시도 자체(=추가 Codex 호출)가 줄어든다.
    """
    problems = []

    if len(items) != 10:
        problems.append(f"문항이 정확히 10개가 아니라 {len(items)}개입니다.")
        return problems

    keys = [item["question_key"] for item in items]
    duplicate_keys = {key for key in keys if keys.count(key) > 1}
    if duplicate_keys:
        problems.append(
            "다음 핵심을 묻는 문항이 서로 중복됩니다: "
            + ", ".join(sorted(duplicate_keys))
        )

    for index, item in enumerate(items, start=1):
        choices = item.get("choices", {})
        if len(choices) != 4:
            problems.append(f"{index}번 문항의 선택지가 4개가 아닙니다.")
        if item.get("correct_choice") not in choices:
            problems.append(f"{index}번 문항의 정답 표시가 선택지 중에 없습니다.")
        if len((item.get("explanation") or "").strip()) < 20:
            problems.append(f"{index}번 문항의 해설이 너무 짧습니다(20자 이상 필요).")
        if _contains_meta_question(item):
            problems.append(
                f"{index}번 문항이 자료 관리·출처(정리본/기출/교수님 언급 등)를 "
                "묻고 있어 실제 시험 문항이 아닙니다."
            )

    answer_counts = {
        choice: sum(item.get("correct_choice") == choice for item in items)
        for choice in "ABCD"
    }
    imbalanced = {
        choice: count
        for choice, count in answer_counts.items()
        if not (1 <= count <= 4)
    }
    if imbalanced:
        problems.append(
            "정답이 A~D에 고르게 분산되지 않았습니다 (현재 분포: "
            + ", ".join(f"{choice}={count}" for choice, count in answer_counts.items())
            + ")."
        )

    return problems


def _quiz_is_valid(items):
    return not _quiz_problems(items)


def build_multiple_choice_quiz_request(summary, exam_texts=()):
    """PDF용 단답형 체크리스트와 별개인 웹 객관식 문제 요청을 만든다."""
    return f"""
아래 의학 강의 정리본을 바탕으로 웹에서 자동 채점할 4지선다형 복습 문제를
정확히 10개 만드세요. PDF의 단답형 체크리스트를 그대로 복사하지 말고,
강의 핵심 개념과 기출 분석을 바탕으로 구별·순서·예외·적용 문제를 고르게 만드세요.

반드시 다음 형식을 정확히 지키세요.

## 6. 시험 직전 체크리스트

1. 질문
A. 선택지
B. 선택지
C. 선택지
D. 선택지
**정답:** B
**해설:** 정답인 이유와 가장 헷갈리는 오답의 구별 기준

요구사항:
- 문제는 정확히 10개
- 모든 문항은 서로 다른 핵심을 평가
- 정답은 A-D에 고르게 분산
- 선택지는 하나만 명확하게 정답이 되도록 작성
- 오답은 실제로 혼동하기 쉬운 같은 범주의 개념으로 작성
- 정리본에 없는 사실은 추가하지 않기
- 기출 분석이 있으면 반복 개념과 출제 방식을 우선 반영하되 문항을 그대로 복사하지 않기
- 정답 표시는 A, B, C, D 중 하나만 작성
- 6번 섹션만 Markdown으로 출력

{QUIZ_QUALITY_RULES}

{QUIZ_QUALITY_EXAMPLE}

{build_exam_style_instruction(exam_texts)}

강의 정리본:
{summary}
""".strip()


def _retry_feedback(problems):
    return (
        "\n\n이전 출력이 아래 이유로 품질 검사에서 탈락했습니다. 지적된 "
        "부분만 정확히 고쳐 10문항 전체를 다시 작성하세요.\n"
        + "\n".join(f"- {problem}" for problem in problems)
    )


def generate_base_multiple_choice_quiz(codex_path, summary, exam_texts=()):
    request = build_multiple_choice_quiz_request(summary, exam_texts=exam_texts)
    problems = []
    output_language = detect_quiz_language(exam_texts)

    for attempt in range(3):
        content = generate_summary_with_codex(
            codex_path,
            request + (_retry_feedback(problems) if problems else ""),
            image_paths=[],
            timeout_seconds=600,
            output_language=output_language,
        )
        items = extract_review_quiz(content)
        problems = _quiz_problems(items)
        if not problems:
            return delatexify(content)

    raise RuntimeError(
        "웹 객관식 문제 형식 검증에 실패했습니다: " + "; ".join(problems)
    )


def build_additional_quiz_request(
    summary, existing_questions, exam_source, exam_texts=()
):
    return f"""
아래 강의 정리본과 과목 기출을 바탕으로 새로운 4지선다형 복습 문제를
정확히 10개 만드세요. 기존 문제와 같은 질문, 같은 판단 기준 또는 숫자만
바꾼 사실상 동일한 문제는 만들지 마세요.

반드시 다음 형식을 정확히 지키세요.

## 6. 시험 직전 체크리스트

1. 질문
A. 선택지
B. 선택지
C. 선택지
D. 선택지
**정답:** B
**해설:** 정답 근거, 매력적인 오답의 오류, 시험장에서의 판별 기준

요구사항:
- 문제는 정확히 10개
- 정답은 A-D에 고르게 분산
- 현재 강의 범위에서만 출제
- 기출 문항을 그대로 복사하지 말고 같은 개념을 새로운 상황과 판단 방식으로 묻기
- 정답 표시는 A, B, C, D 중 하나만 작성
- 6번 섹션만 Markdown으로 출력

{QUIZ_QUALITY_RULES}

{QUIZ_QUALITY_EXAMPLE}

{build_exam_style_instruction(exam_texts)}

이미 출제된 문제:
{existing_questions}

강의 정리본:
{summary}

과목 기출 PDF 추출 텍스트:
{exam_source}
""".strip()


def validate_multiple_choice_quiz(content):
    return _quiz_is_valid(extract_review_quiz(content))


def generate_additional_multiple_choice_quiz(
    codex_path, summary, existing_questions, exam_source, exam_texts=()
):
    """새 문제도 형식뿐 아니라 메타 문항 금지 검사까지 통과할 때만 반환한다."""
    request = build_additional_quiz_request(
        summary, existing_questions, exam_source, exam_texts=exam_texts
    )
    problems = []
    output_language = detect_quiz_language(exam_texts)

    for attempt in range(3):
        content = generate_summary_with_codex(
            codex_path,
            request + (_retry_feedback(problems) if problems else ""),
            image_paths=[],
            timeout_seconds=600,
            output_language=output_language,
        )
        problems = _quiz_problems(extract_review_quiz(content))
        if not problems:
            return delatexify(content)

    raise RuntimeError(
        "실제 시험형 객관식 문제 품질 검증에 연속 실패했습니다: "
        + "; ".join(problems)
    )
