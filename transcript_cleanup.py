"""Whisper의 명백한 반복 환각을 정상 문장 손실 없이 정리한다."""
import re


REPETITION_MARKER = "[음성 인식 불가 구간: 반복 음성]"


def _repeated_word_spans(text):
    matches = list(re.finditer(r"\S+", text))
    words = [match.group(0) for match in matches]
    spans = []
    index = 0
    while index < len(words):
        best = None
        for width in range(1, min(10, (len(words) - index) // 4) + 1):
            unit = words[index:index + width]
            repetitions = 1
            while (
                index + (repetitions + 1) * width <= len(words)
                and words[index + repetitions * width:index + (repetitions + 1) * width]
                == unit
            ):
                repetitions += 1
            minimum = 6 if width == 1 else 4
            if repetitions >= minimum:
                consumed = width * repetitions
                if best is None or consumed > best[0]:
                    best = (consumed, index + consumed)
        if best is None:
            index += 1
            continue
        _, end_word = best
        spans.append((matches[index].start(), matches[end_word - 1].end()))
        index = end_word
    return spans


def sanitize_transcript_text(text):
    """연속 반복과 비정상적으로 긴 확인 필요 표시만 보수적으로 축약한다."""
    if not text:
        return text
    spans = _repeated_word_spans(text)
    if spans:
        parts = []
        cursor = 0
        for start, end in spans:
            parts.extend((text[cursor:start], REPETITION_MARKER))
            cursor = end
        parts.append(text[cursor:])
        text = "".join(parts)

    text = re.sub(
        r"\[확인 필요:\s*\[음성 인식 불가 구간: 반복 음성\][^\]]*\]",
        REPETITION_MARKER,
        text,
    )

    def shorten_uncertain(match):
        content = " ".join(match.group(1).split())
        if len(content) <= 120:
            return match.group(0)
        return f"[확인 필요: {content[:100].rstrip()}… — 긴 반복/불명확 음성]"

    text = re.sub(
        r"\[확인 필요:\s*([^\[\]]{121,})\]",
        shorten_uncertain,
        text,
        flags=re.DOTALL,
    )
    return text
