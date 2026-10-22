import hashlib
import re


SUPERSCRIPT_MAP = str.maketrans({
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
})
SUBSCRIPT_MAP = str.maketrans({
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
    "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "o": "ₒ", "x": "ₓ", "h": "ₕ",
    "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ", "p": "ₚ",
    "s": "ₛ", "t": "ₜ",
})
LATEX_MATH_DELIMITER = re.compile(r"\\[()\[\]]|\$\$?")
# 중괄호로 감싼 형태(`^{-4}`, `_{a}`)만 다룬다. `_x`, `^2`처럼 중괄호
# 없는 한 글자짜리 표기까지 다루면, `EXAM_FIGURE`처럼 그냥 밑줄이 들어간
# 일반 텍스트나 식별자까지 아래첨자로 오인해 밑줄 뒤 글자만 남기고
# 지워버린다(예: "EXAM_FIGURE" -> "EXAMFIGURE"). 중괄호 형태는 진짜
# LaTeX가 아니면 거의 나타나지 않아 안전하다.
LATEX_SUPERSCRIPT = re.compile(r"\^\{([^{}]*)\}")
LATEX_SUBSCRIPT = re.compile(r"_\{([^{}]*)\}")
LATEX_FRAC = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
LATEX_WRAPPER = re.compile(r"\\(?:mathrm|mathbf|mathit|text)\{([^{}]*)\}")
LATEX_SYMBOL_MAP = {
    r"\times": "×",
    r"\cdot": "·",
    r"\pm": "±",
    r"\mp": "∓",
    r"\leq": "≤",
    r"\geq": "≥",
    r"\neq": "≠",
    r"\approx": "≈",
    r"\infty": "∞",
    r"\rightleftharpoons": "⇌",
    r"\rightarrow": "→",
    r"\to": "→",
    r"\,": " ",
    r"\;": " ",
    r"\ ": " ",
    r"\log": "log",
    r"\ln": "ln",
    r"\sin": "sin",
    r"\cos": "cos",
    r"\tan": "tan",
    r"\sqrt": "√",
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\Delta": "Δ",
    r"\mu": "μ",
    # 크기만 조절하는 `\left`/`\right`는 뒤에 오는 진짜 괄호 문자만 남기면
    # 되므로, 더 구체적인 명령(`\rightarrow` 등)을 먼저 치환한 뒤 마지막에
    # 지운다. 순서를 바꾸면 `\rightarrow`의 `\right` 부분까지 먼저
    # 지워져 "arrow"라는 글자만 남는 식으로 망가진다.
    r"\left": "",
    r"\right": "",
}


def delatexify(text):
    """AI가 가끔 섞어 쓰는 LaTeX 표기를 일반 텍스트로 바꾼다.

    Streamlit(및 클라우드 학습 앱)의 Markdown 렌더러는 `\\(...\\)`,
    `\\[...\\]` 같은 LaTeX 수식 구분자를 수식으로 인식하지 못한다. 오히려
    일반 Markdown 이스케이프 규칙 때문에 `\\(`의 백슬래시만 사라지고
    `\\times`, `\\mathrm{...}` 같은 명령어는 글자 그대로 남아 화면에
    깨진 채로 보인다. 화학·물리 문제에 흔한 표기(지수, 아래첨자, ×, 분수,
    로그 등)만 다루는 실용적인 변환이며, 완전한 LaTeX 파서는 아니다.
    """
    if not text or "\\" not in text and "^" not in text and "_" not in text:
        return text

    result = LATEX_FRAC.sub(lambda m: f"{m.group(1)}/{m.group(2)}", text)
    result = LATEX_WRAPPER.sub(lambda m: m.group(1), result)

    for command, symbol in LATEX_SYMBOL_MAP.items():
        result = result.replace(command, symbol)

    result = LATEX_SUPERSCRIPT.sub(
        lambda m: m.group(1).translate(SUPERSCRIPT_MAP), result
    )
    result = LATEX_SUBSCRIPT.sub(
        lambda m: m.group(1).translate(SUBSCRIPT_MAP), result
    )
    result = LATEX_MATH_DELIMITER.sub("", result)
    return result


SECTION_HEADING_PATTERN = re.compile(
    r"^#{1,6}\s*6[.\s]+시험\s*직전\s*체크리스트\s*$",
    re.MULTILINE,
)
NEXT_SECTION_PATTERN = re.compile(
    r"^#{1,6}\s*7[.\s]+.*$",
    re.MULTILINE,
)
QUESTION_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?"
    r"(?:Q\s*)?(\d{1,2})[.)]\s*"
    r"(.+?)(?:\*\*)?\s*$",
    re.IGNORECASE,
)
ANSWER_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?정답(?:\*\*)?\s*:\s*(.*)$",
    re.IGNORECASE,
)
CHOICE_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?([A-E])[.)]\s*(.+?)\s*$",
    re.IGNORECASE,
)
EXPLANATION_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?해설(?:\*\*)?\s*:\s*(.*)$",
    re.IGNORECASE,
)
# 기출이 5지선다인 과목은 복습 문제도 A~E 다섯 개로 만든다.
CHOICE_KEYS = ("A", "B", "C", "D", "E")
# `ㄱ. ...`, `ㄴ. ...`처럼 <보기> 진술이 시작되는 지점. 파싱하면 질문이 한
# 줄로 합쳐지므로, 화면에 그릴 때 이 앞에서 다시 줄을 바꾼다.
BOGI_ITEM_START = re.compile(r"\s+(?=[ㄱㄴㄷㄹㅁㅂ]\.\s)")


def quiz_choice_keys(item):
    """문항에 실제로 있는 선택지 키를 순서대로(4지선다 A~D, 5지선다 A~E)."""
    choices = item.get("choices", {})
    return [key for key in CHOICE_KEYS if key in choices]


def is_multiple_choice_item(item):
    """A부터 빠짐없이 4개 또는 5개의 선택지와 정답 표시가 있는 객관식 문항."""
    choices = item.get("choices", {})
    return (
        len(choices) in (4, 5)
        and quiz_choice_keys(item) == list(CHOICE_KEYS[:len(choices)])
        and item.get("correct_choice") in choices
    )


def question_markdown(question):
    """<보기> 진술(ㄱ. ㄴ. ㄷ.)을 한 줄씩 보이도록 줄바꿈을 넣는다."""
    return BOGI_ITEM_START.sub("  \n", question)


def _clean_markdown_text(text):
    text = text.strip()
    text = text.replace("  \n", " ")
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _question_key(question):
    normalized = re.sub(r"\s+", " ", question).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def extract_review_quiz(summary):
    """AI 정리본의 6번 체크리스트에서 문제와 정답을 추출한다."""
    section_match = SECTION_HEADING_PATTERN.search(summary)

    if not section_match:
        return []

    section = summary[section_match.end():]
    next_section = NEXT_SECTION_PATTERN.search(section)

    if next_section:
        section = section[:next_section.start()]

    quizzes = []
    current = None

    for raw_line in section.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        question_match = QUESTION_PATTERN.match(line)
        answer_match = ANSWER_PATTERN.match(line)
        choice_match = CHOICE_PATTERN.match(line)
        explanation_match = EXPLANATION_PATTERN.match(line)

        if question_match and not answer_match:
            if current and current.get("question") and current.get("answer"):
                quizzes.append(current)

            question = _clean_markdown_text(question_match.group(2))
            current = {
                "number": int(question_match.group(1)),
                "question": question,
                "answer": "",
                "choices": {},
                "correct_choice": "",
                "explanation": "",
                "question_key": _question_key(question),
            }
            continue

        if current is None:
            continue

        # `E. coli ...`처럼 E로 시작하는 질문·해설 줄을 선택지로 오인하지
        # 않도록, E는 A~D가 다 나온 뒤 정답 줄 전에만 선택지로 받는다.
        if (
            choice_match
            and choice_match.group(1).upper() == "E"
            and (
                current["answer"]
                or len(current["choices"]) != 4
            )
        ):
            choice_match = None

        if choice_match:
            choice_key = choice_match.group(1).upper()
            current["choices"][choice_key] = _clean_markdown_text(
                choice_match.group(2)
            )
        elif answer_match:
            answer = _clean_markdown_text(answer_match.group(1))
            current["answer"] = answer
            answer_choice = re.match(r"^([A-E])(?:[.)\s]|$)", answer, re.I)
            if answer_choice:
                current["correct_choice"] = answer_choice.group(1).upper()
        elif explanation_match:
            current["explanation"] = _clean_markdown_text(
                explanation_match.group(1)
            )
        elif current["answer"]:
            if current["explanation"]:
                current["explanation"] = _clean_markdown_text(
                    f'{current["explanation"]} {line}'
                )
            else:
                current["answer"] = _clean_markdown_text(
                    f'{current["answer"]} {line}'
                )
        else:
            current["question"] = _clean_markdown_text(
                f'{current["question"]} {line}'
            )
            current["question_key"] = _question_key(current["question"])

    if current and current.get("question") and current.get("answer"):
        quizzes.append(current)

    for quiz in quizzes:
        if quiz["correct_choice"] in quiz["choices"]:
            quiz["answer_text"] = quiz["choices"][quiz["correct_choice"]]
        else:
            quiz["answer_text"] = quiz["answer"]

    quizzes.sort(key=lambda item: item["number"])
    return quizzes


def replace_review_quiz_section(summary, replacement):
    """정리본의 6번 체크리스트만 새 객관식 섹션으로 교체한다."""
    section_match = SECTION_HEADING_PATTERN.search(summary)

    if not section_match:
        raise ValueError("시험 직전 체크리스트를 찾지 못했습니다.")

    remainder = summary[section_match.end():]
    next_section = NEXT_SECTION_PATTERN.search(remainder)

    if not next_section:
        raise ValueError("체크리스트 다음 7번 섹션을 찾지 못했습니다.")

    replacement = replacement.strip()
    replacement = re.sub(
        r"^.*?(?=#{1,6}\s*6[.\s]+시험\s*직전\s*체크리스트)",
        "",
        replacement,
        flags=re.DOTALL,
    )
    replacement = re.sub(
        r"\n#{1,6}\s*7[.\s]+.*$",
        "",
        replacement,
        flags=re.DOTALL,
    ).strip()

    if not SECTION_HEADING_PATTERN.search(replacement):
        replacement = "## 6. 시험 직전 체크리스트\n\n" + replacement

    return (
        summary[:section_match.start()]
        + replacement
        + "\n\n"
        + remainder[next_section.start():]
    )


def build_short_answer_quiz_section(quiz_items):
    """객관식 웹 문항을 PDF용 주관식·단답형 체크리스트로 바꾼다."""
    lines = ["## 6. 시험 직전 체크리스트", ""]

    for index, item in enumerate(quiz_items, start=1):
        answer_text = item.get("answer_text") or item.get("answer", "")
        lines.extend(
            [
                f'{index}. {item["question"]}',
                "",
                f"**정답:** {answer_text}",
                "",
            ]
        )

    return "\n".join(lines).strip()
