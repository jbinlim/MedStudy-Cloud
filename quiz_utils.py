import hashlib
import re


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
    r"^\s*(?:[-*]\s*)?([A-D])[.)]\s*(.+?)\s*$",
    re.IGNORECASE,
)
EXPLANATION_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?해설(?:\*\*)?\s*:\s*(.*)$",
    re.IGNORECASE,
)


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

        if choice_match:
            choice_key = choice_match.group(1).upper()
            current["choices"][choice_key] = _clean_markdown_text(
                choice_match.group(2)
            )
        elif answer_match:
            answer = _clean_markdown_text(answer_match.group(1))
            current["answer"] = answer
            answer_choice = re.match(r"^([A-D])(?:[.)\s]|$)", answer, re.I)
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
