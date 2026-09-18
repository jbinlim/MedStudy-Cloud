import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageStat
from codex_service import CODEX_MODEL, CODEX_REASONING_EFFORT


# 슬라이드에 실제 사진(래스터 이미지)으로 삽입된 그림은, 파워포인트 도형
# 기능으로 그린 벡터 다이어그램과 달리 PDF 안에 이미지 객체로 그대로
# 들어 있어 PyMuPDF로 좌표까지 결정된 채 곧바로 잘라낼 수 있다. 이 최소
# 크기보다 작은 이미지는 아이콘·장식 요소일 가능성이 높아 후보에서
# 제외한다.
EMBEDDED_FIGURE_MIN_WIDTH = 120
EMBEDDED_FIGURE_MIN_HEIGHT = 90

# 페이지 거의 전체를 덮는 이미지 객체는 장식용 배경일 가능성이 매우
# 높고, get_pixmap(clip=rect)로 잘라내도 그 위에 합성된 페이지 전체
# (그림+본문 텍스트 전부)가 그대로 찍혀 나온다. Codex 판별 없이도
# 항상 걸러도 안전하다.
EMBEDDED_FIGURE_MAX_AREA_RATIO = 0.97

CLASSIFY_FIGURE_SCHEMA = {
    "type": "object",
    "properties": {
        "figures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "include": {"type": "boolean"},
                    "caption": {"type": "string"},
                },
                "required": ["index", "include", "caption"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["figures"],
    "additionalProperties": False,
}


def _rendered_pixmap_has_visible_content(pixmap, minimum_ratio=0.004):
    """실제로 화면에 렌더링된 픽셀을 기준으로 빈 배경 이미지를 거른다.

    같은 판단을 PDF에 남아 있는 원본 이미지 객체(`pymupdf.Pixmap(document,
    xref)`)로 하면 안 된다. 파워포인트에서 자른(crop) 사진은 원본 객체에
    잘려나가 화면에는 안 보이는 픽셀이 그대로 남아 있을 수 있는데, 실제로
    이 강의록의 DNA 구조 그림(대부분 흰 배경 + 선 그림)이 원본 객체
    기준으로는 평균 밝기가 매우 낮게 나와 "빈 배경"으로 잘못 걸러졌다.
    반드시 저장에 쓰는 것과 같은 `page.get_pixmap(clip=rect)` 렌더링
    결과로 판단해야 화면에 실제로 보이는 내용과 일치한다.
    """
    channels = pixmap.n
    color_channels = min(3, channels)
    pixel_count = max(1, pixmap.width * pixmap.height)
    samples = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(-1, channels)
    darkest = samples[:, :color_channels].min(axis=1)
    visible = int((darkest < 240).sum())
    light = int((darkest > 245).sum())
    return (
        visible / pixel_count >= minimum_ratio
        and light / pixel_count >= 0.20
    )


def extract_embedded_figure_candidates(pdf_path, page_records, output_dir):
    """PDF에 실제 이미지 객체로 삽입된 그림을 AI 없이 좌표째 잘라낸다.

    파워포인트에서 사진을 그대로 붙여 넣은 슬라이드는 PDF 안에도 이미지
    객체로 남아 있어, 어디가 그림인지 AI에게 좌표를 맡길 필요가 없다.
    이 후보만 먼저 뽑아 두면, 뒤이은 AI 호출은 "이 그림이 쓸모 있는지
    판단 + 캡션 작성"만 하면 되어(좌표 추정 불필요) 페이지 전체 대신
    이미 잘라낸 작은 이미지만 보내도 된다 — 이미지가 작을수록 토큰이
    적게 들어 비용이 줄어든다. 도형·선으로 그린 벡터 다이어그램은 PDF
    안에 이미지 객체가 아니라 그리기 명령으로 남아 있어 이 방식으로는
    찾을 수 없으므로, 그런 페이지는 기존 방식(전체 페이지를 AI에게 보내
    좌표까지 찾게 하는 `detect_figure_regions_with_codex`)으로 처리해야
    한다.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pages_by_number = {page["page_no"]: page for page in page_records}
    candidates = []

    with pymupdf.open(pdf_path) as document:
        for page_no, page in enumerate(document, start=1):
            if page_no not in pages_by_number:
                continue

            seen_xrefs = set()
            region_index = 0
            for image in page.get_images(full=True):
                xref = image[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)

                for rect in page.get_image_rects(xref):
                    if (
                        rect.width < EMBEDDED_FIGURE_MIN_WIDTH
                        or rect.height < EMBEDDED_FIGURE_MIN_HEIGHT
                    ):
                        continue

                    page_area = page.rect.width * page.rect.height
                    if (
                        page_area > 0
                        and (rect.width * rect.height) / page_area
                        > EMBEDDED_FIGURE_MAX_AREA_RATIO
                    ):
                        continue

                    # 원본 이미지 객체를 직접 꺼내지 않고, 실제로 화면에
                    # 보이는 페이지 합성 상태를 렌더링해서 그 결과로
                    # 빈 배경 여부까지 판단하고 그대로 저장한다(파워
                    # 포인트에서 자른 사진은 원본 객체에 잘려나간 부분이
                    # 그대로 남아 있을 수 있다 — 기출 이미지 크롭에서 이미
                    # 겪은 문제와 같다).
                    try:
                        pixmap = page.get_pixmap(
                            matrix=pymupdf.Matrix(2, 2), clip=rect, alpha=False
                        )
                        if not _rendered_pixmap_has_visible_content(pixmap):
                            continue
                    except Exception:
                        continue

                    region_index += 1
                    figure_key = f"p{page_no:03d}_e{region_index:02d}"
                    figure_path = output_dir / f"{figure_key}.jpg"
                    pixmap.pil_save(figure_path, format="JPEG", quality=92)

                    candidates.append({
                        "figure_key": figure_key,
                        "page_no": page_no,
                        "image_path": str(figure_path),
                    })

    return candidates


def classify_figure_candidates_with_codex(
    codex_path,
    candidates,
    timeout_seconds=600,
):
    """추출된 후보 그림 중 학습 정리본에 넣을 가치가 있는 것만 고른다.

    페이지 전체를 보내 좌표까지 찾게 하는 `detect_figure_regions_with_codex`와
    달리, 이미 잘라낸 작은 이미지만 보내 "포함할지 + 캡션"만 판단하게
    한다.
    """
    if not candidates:
        return []

    figure_mapping = "\n".join(
        f"- 첨부 {index}: 후보 그림"
        for index in range(1, len(candidates) + 1)
    )
    prompt = f"""
첨부된 이미지는 의대 강의 슬라이드에서 이미 잘라낸 그림 후보입니다. 각
후보가 학습 정리본에 넣을 가치가 있는지 판단하세요.

첨부 순서:
{figure_mapping}

각 첨부는 서로 완전히 독립적으로 판단하세요. 다른 첨부가 제외 대상(예:
시험 문제 이미지)이라 해서 함께 있는 다른 첨부까지 덩달아 엄격하게
보거나 낮게 평가하지 마세요. 각 이미지 하나하나를 그 자체의 학습
가치만으로 판단하세요.

포함 대상:
- 해부학 구조 그림, 임상 사진, 과정 도식, 경로도, 그래프
- 개념·용어 사이의 관계를 화살표나 도형으로 보여주는 다이어그램(글자와
  도형이 섞여 있어도, 관계 자체가 정보라면 포함하세요)

제외 대상(`include`를 false로):
- 글자만 나열되어 있고 그림·도형이 전혀 없는 이미지
- 슬라이드 전체를 그대로 캡처한 듯한 이미지인데, 실제 그림(사진·도식)
  보다 본문 글머리표·정의·설명 문장이 차지하는 면적이 더 넓은 경우.
  슬라이드 어딘가에 그림이 포함되어 있다는 이유만으로 포함하지
  마세요 — 그림이 이미지 안에서 실제로 주된 피사체를 이뤄야 포함
  대상입니다.
- 로고, 장식 요소, 배경 무늬, 페이지 번호
- 정보 전달력이 전혀 없는 장식용 도형
- 시험 문제 원문을 찍거나 캡처한 이미지(문제 번호, 선택지 `①②③④⑤`,
  `객관식` 표기 등이 보이는 경우). 기출문제는 별도로 처리되므로 여기서는
  항상 제외하세요.

포함하는 항목만 caption을 그림이 보여주는 핵심 내용을 한국어 한 문장으로
작성하세요. 제외하는 항목은 caption을 빈 문자열로 두세요.
index는 첨부 순서(1부터 시작)와 정확히 일치해야 하며, 모든 첨부에 대해
결과를 하나씩 반환하세요.
""".strip()

    with tempfile.TemporaryDirectory(
        prefix="medstudy-figure-classify-"
    ) as temp_dir:
        temp_path = Path(temp_dir)
        schema_path = temp_path / "classify_schema.json"
        output_path = temp_path / "classify.json"
        schema_path.write_text(
            json.dumps(CLASSIFY_FIGURE_SCHEMA, ensure_ascii=False),
            encoding="utf-8",
        )

        command = [
            codex_path,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--model",
            CODEX_MODEL,
            "--config",
            f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--cd",
            temp_dir,
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]

        for candidate in candidates:
            command.extend(["--image", candidate["image_path"]])

        command.append("-")

        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

        if result.returncode != 0:
            error_message = (
                result.stderr.strip()
                or result.stdout.strip()
                or "알 수 없는 그림 분류 오류"
            )
            raise RuntimeError(error_message[-2000:])

        if not output_path.exists():
            raise RuntimeError(
                "Codex가 그림 분류 결과를 만들지 못했습니다."
            )

        decisions = json.loads(
            output_path.read_text(encoding="utf-8")
        )["figures"]

    decisions_by_index = {
        decision["index"]: decision for decision in decisions
    }
    accepted = []
    for position, candidate in enumerate(candidates, start=1):
        decision = decisions_by_index.get(position)
        if not decision or not decision.get("include"):
            continue
        caption = (decision.get("caption") or "").strip()
        if not caption:
            continue
        accepted.append({
            **candidate,
            "caption": caption,
            "x0": 0,
            "y0": 0,
            "x1": 1000,
            "y1": 1000,
        })

    return accepted


FIGURE_SCHEMA = {
    "type": "object",
    "properties": {
        "figures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page_no": {"type": "integer"},
                    "x0": {"type": "integer"},
                    "y0": {"type": "integer"},
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "caption": {"type": "string"},
                },
                "required": [
                    "page_no",
                    "x0",
                    "y0",
                    "x1",
                    "y1",
                    "caption",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["figures"],
    "additionalProperties": False,
}


def select_figure_candidate_pages(page_records):
    """명백한 텍스트 전용 슬라이드를 AI 그림 검사에서 제외한다.

    애매한 페이지는 후보에 남겨 중요한 흑백 도식을 놓치지 않도록 한다.
    """
    candidates = []

    for page in page_records:
        text = (
            (page.get("embedded_text") or "")
            or (page.get("text") or "")
        ).strip()
        image_path = Path(page.get("image_path") or "")

        if not image_path.exists():
            continue

        # 텍스트가 적거나 OCR 기반인 페이지는 그림 가능성이 있으므로 유지한다.
        if len(text) <= 1600 or not page.get("embedded_text"):
            candidates.append(page)
            continue

        # 텍스트가 많은 페이지라도 컬러 사진/도식이 있으면 후보에 남긴다.
        with Image.open(image_path) as image:
            thumbnail = image.convert("RGB")
            thumbnail.thumbnail((240, 240))
            saturation = thumbnail.convert("HSV").getchannel("S")
            mean_saturation = ImageStat.Stat(saturation).mean[0]

        if mean_saturation >= 12:
            candidates.append(page)

    return candidates


def detect_figure_regions_with_codex(
    codex_path,
    page_records,
    timeout_seconds=600,
):
    page_mapping = "\n".join(
        (
            f'- 첨부 {index}: PDF {page["page_no"]}페이지'
        )
        for index, page in enumerate(
            page_records,
            start=1,
        )
    )
    prompt = f"""
첨부된 의대 강의 슬라이드에서 학습 정리본에 삽입할 가치가 있는
그림 영역만 찾으세요.

첨부 순서와 PDF 페이지:
{page_mapping}

선택 대상:
- 해부학 구조 그림, 임상 사진, 과정 도식, 경로도, 그래프
- 글과 함께 있어도 그림 자체가 개념 이해에 꼭 필요한 영역

제외 대상:
- 글자만 있는 슬라이드
- 제목, 본문, 문제와 선택지만 있는 영역
- 단순 표지, 로고, 페이지 번호, 장식 요소
- 그림 없이 텍스트만 정렬한 표

각 그림의 경계 상자를 이미지 전체 기준 0-1000 정규화 좌표로 반환하세요.
가능하면 제목과 긴 설명 문장은 제외하고 그림 전체와 그림 안의 필수 라벨은 포함하세요.
한 페이지에 독립적인 그림이 여러 개면 따로 반환하세요.
가치 있는 그림이 없으면 그 페이지에서는 아무것도 반환하지 마세요.
caption은 그림이 보여주는 핵심 내용을 한국어 한 문장으로 작성하세요.
""".strip()

    with tempfile.TemporaryDirectory(
        prefix="medstudy-figure-detect-"
    ) as temp_dir:
        temp_path = Path(temp_dir)
        schema_path = temp_path / "figure_schema.json"
        output_path = temp_path / "figures.json"
        schema_path.write_text(
            json.dumps(FIGURE_SCHEMA, ensure_ascii=False),
            encoding="utf-8",
        )

        command = [
            codex_path,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--model",
            CODEX_MODEL,
            "--config",
            f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--cd",
            temp_dir,
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]

        for page in page_records:
            command.extend(
                ["--image", page["image_path"]]
            )

        command.append("-")

        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

        if result.returncode != 0:
            error_message = (
                result.stderr.strip()
                or result.stdout.strip()
                or "알 수 없는 그림 탐지 오류"
            )
            raise RuntimeError(error_message[-2000:])

        if not output_path.exists():
            raise RuntimeError(
                "Codex가 그림 탐지 결과를 만들지 못했습니다."
            )

        return json.loads(
            output_path.read_text(encoding="utf-8")
        )["figures"]


def crop_detected_figures(
    detections,
    page_records,
    output_dir,
):
    pages_by_number = {
        page["page_no"]: page
        for page in page_records
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cropped_figures = []
    page_figure_counts = {}

    for detection in detections:
        page_no = detection["page_no"]
        page = pages_by_number.get(page_no)

        if not page:
            continue

        x0 = max(0, min(1000, detection["x0"]))
        y0 = max(0, min(1000, detection["y0"]))
        x1 = max(0, min(1000, detection["x1"]))
        y1 = max(0, min(1000, detection["y1"]))

        if x1 - x0 < 80 or y1 - y0 < 80:
            continue

        area_ratio = (
            (x1 - x0) * (y1 - y0) / 1_000_000
        )

        if area_ratio > 0.88:
            continue

        image_path = Path(page["image_path"])

        with Image.open(image_path) as image:
            width, height = image.size
            padding_x = int(width * 0.012)
            padding_y = int(height * 0.012)
            crop_box = (
                max(0, int(width * x0 / 1000) - padding_x),
                max(0, int(height * y0 / 1000) - padding_y),
                min(width, int(width * x1 / 1000) + padding_x),
                min(height, int(height * y1 / 1000) + padding_y),
            )
            cropped = image.crop(crop_box)

            figure_no = (
                page_figure_counts.get(page_no, 0) + 1
            )
            page_figure_counts[page_no] = figure_no
            figure_key = f"p{page_no:03d}_f{figure_no:02d}"
            figure_path = output_dir / f"{figure_key}.jpg"
            cropped.save(
                figure_path,
                format="JPEG",
                quality=92,
                optimize=True,
            )

        cropped_figures.append(
            {
                "figure_key": figure_key,
                "page_no": page_no,
                "image_path": str(figure_path),
                "caption": detection["caption"].strip(),
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
            }
        )

    return cropped_figures
