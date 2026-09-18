import re

from codex_service import generate_summary_with_codex


def _relevant_summary_part(content):
    match = re.search(r"^##\s+5[.]\s+", content, re.MULTILINE)
    return content[:match.start()] if match else content


def _apply_exact_phrases(content, phrases):
    section_four = re.search(r"^##\s+4[.]\s+", content, re.MULTILINE)
    if not section_four:
        return content, []

    front = content[:section_four.start()]
    remainder = content[section_four.start():]
    applied = []

    for phrase in phrases:
        if (
            len(phrase) < 8
            or "[[EXAM_HIGHLIGHT:" in phrase
            or phrase not in front
        ):
            continue
        front = front.replace(
            phrase,
            f"[[EXAM_HIGHLIGHT:{phrase}]]",
            1,
        )
        applied.append(phrase)

    return front + remainder, applied


def highlight_summary_with_codex(codex_path, content):
    """기출 분석과 직접 일치하는 기존 원문 구절에만 표식을 추가한다."""
    request = f"""
아래 Markdown 정리본의 1-3번 섹션에서, 4번 기출문제 분석에 의해 실제 출제 사실임이
명확하게 확인되는 기존 문장 또는 구절만 고르세요.

규칙:
- 중요하거나 출제 가능성이 있다는 추정만으로 고르지 마세요.
- 4번 기출문제 분석에 명시된 개념과 직접 일치해야 합니다.
- 1-3번 섹션에 존재하는 문자열을 글자, 띄어쓰기, Markdown 기호까지 한 글자도 바꾸지
  말고 그대로 복사하세요.
- 글머리표의 `- `는 제외하고 실제 내용만 복사하세요.
- 한 항목 전체가 길면 실제 출제된 핵심 문장 또는 핵심 구절만 고르세요.
- 설명, 번호, 코드 블록 없이 각 줄을 `HIGHLIGHT: 원문 그대로` 형식으로만 출력하세요.
- 해당하는 내용이 없으면 `NONE`만 출력하세요.

정리본:
{_relevant_summary_part(content)}
""".strip()

    response = generate_summary_with_codex(
        codex_path,
        request,
        image_paths=[],
        timeout_seconds=900,
    )

    phrases = []
    for line in response.splitlines():
        line = line.strip()
        if not line.startswith("HIGHLIGHT:"):
            continue
        phrase = line[len("HIGHLIGHT:"):].strip()
        if phrase not in phrases:
            phrases.append(phrase)

    updated, applied = _apply_exact_phrases(content, phrases)
    return updated, len(applied)
