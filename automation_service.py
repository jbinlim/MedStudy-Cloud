import hashlib
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic

from ai_service import (
    build_chatgpt_request,
    build_exam_analysis_request,
    build_frequent_exam_topics_request,
    find_missing_exam_figures,
    insert_frequent_topics_section,
    insert_missing_exam_sections,
    sanitize_generated_summary,
    split_summary_sections,
)
from exam_highlight_service import highlight_summary_with_codex
from codex_service import (
    CODEX_MODEL,
    CODEX_REASONING_EFFORT,
    generate_summary_with_codex,
)
from database import (
    get_base_web_quiz,
    get_lecture_figures,
    get_lecture_exam_figures,
    get_lecture_pages,
    get_lecture_summary,
    replace_lecture_figures,
    replace_lecture_exam_figures,
    replace_lecture_pages,
    save_lecture_ai_results,
    save_base_web_quiz,
    update_corrected_transcript,
    update_summary_image_pages,
)
from figure_service import (
    classify_figure_candidates_with_codex,
    crop_detected_figures,
    detect_figure_regions_with_codex,
    extract_embedded_figure_candidates,
    select_figure_candidate_pages,
)
from exam_figure_service import prepare_exam_figures
from pdf_utils import process_pdf_pages
from transcript_service import correct_transcript_with_codex
from transcript_cleanup import sanitize_transcript_text
from web_quiz_service import generate_base_multiple_choice_quiz


PDF_CACHE_VERSION = 2
TRANSCRIPT_CACHE_VERSION = 5
FIGURE_CACHE_VERSION = 3
EXAM_FIGURE_CACHE_VERSION = 16
SUMMARY_CACHE_VERSION = 1


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_cache_manifest(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_cache_manifest(path, manifest):
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _rows_to_dicts(rows):
    return [dict(row) for row in rows]


def _all_paths_exist(records, key):
    return bool(records) and all(
        record.get(key) and Path(record[key]).exists()
        for record in records
    )


def run_full_automation(
    *,
    lecture_id,
    subject_name,
    lecture_no,
    lecture_title,
    pdf_path,
    transcript_path,
    ocr_data_dir,
    codex_path,
    progress_callback=None,
):
    """원본 자료부터 최종 AI 정리본까지 전체 파이프라인을 실행한다."""
    pdf_path = Path(pdf_path)
    transcript_path = Path(transcript_path)
    lecture_dir = pdf_path.parent
    cache_path = lecture_dir / "automation_cache.json"
    cache = _load_cache_manifest(cache_path)
    pdf_hash = _file_sha256(pdf_path)
    transcript_hash = _file_sha256(transcript_path)
    reported_progress = 0.0
    automation_started = monotonic()
    timings = {}
    warnings = []

    def report(progress, stage, message):
        nonlocal reported_progress
        reported_progress = max(reported_progress, progress)
        if progress_callback:
            progress_callback(reported_progress, stage, message)

    report(0.02, "1/6", "PDF 페이지 이미지와 텍스트 처리를 준비 중입니다.")

    def update_pdf_progress(current, total):
        report(
            0.03 + (current / total) * 0.27,
            "1/6",
            f"PDF 이미지·OCR 처리 중 · {current}/{total}페이지",
        )

    cached_pages = _rows_to_dicts(get_lecture_pages(lecture_id))
    pdf_cache_valid = (
        cache.get("pdf") == {
            "hash": pdf_hash,
            "version": PDF_CACHE_VERSION,
        }
        and _all_paths_exist(cached_pages, "image_path")
    )

    if pdf_cache_valid:
        page_records = cached_pages
        report(0.30, "1/6", "변경되지 않은 PDF 처리 결과를 재사용했습니다.")
    else:
        pdf_started = monotonic()
        processed_pages = process_pdf_pages(
            pdf_path,
            lecture_dir / "pages",
            ocr_data_dir,
            progress_callback=update_pdf_progress,
        )
        replace_lecture_pages(lecture_id, processed_pages)
        page_records = [
            {
                **page,
                "page_no": page_no,
                "include_in_summary": 0,
            }
            for page_no, page in enumerate(processed_pages, start=1)
        ]
        cache["pdf"] = {
            "hash": pdf_hash,
            "version": PDF_CACHE_VERSION,
        }
        cache.pop("figures", None)
        cache.pop("transcript_correction", None)
        _save_cache_manifest(cache_path, cache)
        timings["PDF·OCR"] = monotonic() - pdf_started

    if pdf_cache_valid:
        timings["PDF·OCR"] = 0.0

    original_transcript = transcript_path.read_text(encoding="utf-8-sig")

    corrected_path = lecture_dir / "corrected_transcript.txt"
    correction_cache_valid = (
        cache.get("transcript_correction") == {
            "pdf_hash": pdf_hash,
            "transcript_hash": transcript_hash,
            "version": TRANSCRIPT_CACHE_VERSION,
        }
        and corrected_path.exists()
    )
    cached_figures = _rows_to_dicts(get_lecture_figures(lecture_id))
    figure_cache_valid = (
        cache.get("figures") == {
            "pdf_hash": pdf_hash,
            "version": FIGURE_CACHE_VERSION,
        }
        and (
            not cached_figures
            or _all_paths_exist(cached_figures, "image_path")
        )
    )

    progress_events = queue.Queue()
    codex_slots = threading.BoundedSemaphore(2)

    def correct_transcript_task():
        started = monotonic()
        try:
            if correction_cache_valid:
                progress_events.put((0.50, "2/6", "기존 전사문 교정 결과를 재사용했습니다."))
                return corrected_path.read_text(encoding="utf-8")

            return correct_transcript_with_codex(
                codex_path,
                original_transcript,
                page_records,
                progress_callback=lambda current, total: progress_events.put((
                    0.30 + ((current - 1) / total) * 0.20,
                    "2/6",
                    f"강의록 기반 전사문 교정 중 · {current}/{total}구간",
                )),
                max_workers=2,
                request_semaphore=codex_slots,
            )
        finally:
            timings["전사문 교정"] = monotonic() - started

    def find_figures_task():
        started = monotonic()
        if figure_cache_valid:
            progress_events.put((0.70, "3/6", "기존 핵심 그림 탐지 결과를 재사용했습니다."))
            timings["그림 탐색"] = monotonic() - started
            return cached_figures

        candidates = select_figure_candidate_pages(page_records)
        progress_events.put((
            0.50,
            "3/6",
            f"그림 후보 선별 완료 · 전체 {len(page_records)}페이지 중 {len(candidates)}페이지",
        ))

        # 슬라이드에 실제 사진(래스터 이미지)으로 삽입된 그림은 PDF 안에
        # 이미지 객체로 그대로 들어 있어, AI 없이 좌표까지 정확히 잘라낼
        # 수 있다. 이 후보가 있는 페이지는 이미 잘라낸 작은 이미지만
        # AI에게 보내 "포함할지 + 캡션"만 판단하게 해서(좌표 추정 불필요)
        # 페이지 전체를 보내는 것보다 이미지 토큰을 훨씬 적게 쓴다. 도형
        # 으로 그린 벡터 다이어그램처럼 이미지 객체가 없는 페이지만 기존
        # 방식(전체 페이지 탐지)으로 처리한다.
        embedded_candidates = extract_embedded_figure_candidates(
            pdf_path, candidates, lecture_dir / "figures"
        )
        progress_events.put((
            0.55,
            "3/6",
            f"삽입 이미지 {len(embedded_candidates)}개 추출",
        ))

        cropped = []

        if embedded_candidates:
            EMBEDDED_CLASSIFY_BATCH_SIZE = 10
            embedded_batches = [
                embedded_candidates[start:start + EMBEDDED_CLASSIFY_BATCH_SIZE]
                for start in range(0, len(embedded_candidates), EMBEDDED_CLASSIFY_BATCH_SIZE)
            ]

            def classify_batch(batch_item):
                batch_no, batch = batch_item
                progress_events.put((
                    0.55 + (batch_no - 1) / max(1, len(embedded_batches)) * 0.10,
                    "3/6",
                    f"삽입 이미지 판별 중 · {batch_no}/{len(embedded_batches)}묶음",
                ))
                with codex_slots:
                    return classify_figure_candidates_with_codex(codex_path, batch)

            batch_items = list(enumerate(embedded_batches, start=1))
            cursor = 0
            while cursor < len(batch_items):
                group = batch_items[cursor:cursor + 2]
                if len(group) == 1:
                    group_results = [classify_batch(group[0])]
                else:
                    with ThreadPoolExecutor(max_workers=2) as batch_executor:
                        group_results = list(batch_executor.map(classify_batch, group))
                for result in group_results:
                    cropped.extend(result)
                cursor += len(group)

        # 임베디드 이미지가 아예 없던 페이지뿐 아니라, 임베디드 후보는
        # 있었지만 분류에서 전부 탈락한 페이지도(예: 슬라이드 전체가
        # 하나의 이미지로 캡처되어 본문 텍스트가 그림보다 많은 경우)
        # 그림 없이 그냥 넘어가지 않고, 좌표까지 직접 찾아 타이트하게
        # 자르는 더 정확한 경로로 한 번 더 시도한다.
        accepted_page_numbers = {figure["page_no"] for figure in cropped}
        vector_only_pages = [
            page for page in candidates
            if page["page_no"] not in accepted_page_numbers
        ]
        progress_events.put((
            0.60,
            "3/6",
            f"전체 페이지 탐지 필요 {len(vector_only_pages)}페이지",
        ))

        if vector_only_pages:
            page_batches = [
                vector_only_pages[start:start + 8]
                for start in range(0, len(vector_only_pages), 8)
            ]
            def detect_batch(batch_item):
                batch_no, page_batch = batch_item
                progress_events.put((
                    0.65 + (batch_no - 1) / max(1, len(page_batches)) * 0.05,
                    "3/6",
                    f"핵심 그림 자동 탐색 중 · {batch_no}/{len(page_batches)}묶음",
                ))
                with codex_slots:
                    return detect_figure_regions_with_codex(codex_path, page_batch)

            detections = []
            batch_items = list(enumerate(page_batches, start=1))
            cursor = 0
            while cursor < len(batch_items):
                worker_count = 2
                group = batch_items[cursor:cursor + worker_count]
                if worker_count == 1:
                    group_results = [detect_batch(group[0])]
                else:
                    with ThreadPoolExecutor(max_workers=2) as batch_executor:
                        group_results = list(batch_executor.map(detect_batch, group))
                for result in group_results:
                    detections.extend(result)
                cursor += len(group)

            cropped.extend(
                crop_detected_figures(
                    detections,
                    page_records,
                    lecture_dir / "figures",
                )
            )

        timings["그림 탐색"] = monotonic() - started
        return cropped

    report(0.30, "2–3/6", "전사문 교정과 핵심 그림 탐색을 동시에 시작합니다.")
    with ThreadPoolExecutor(max_workers=2) as executor:
        correction_future = executor.submit(correct_transcript_task)
        figures_future = executor.submit(find_figures_task)
        while not (correction_future.done() and figures_future.done()):
            try:
                progress, stage, message = progress_events.get(timeout=0.25)
                report(progress, stage, message)
            except queue.Empty:
                pass
        while not progress_events.empty():
            progress, stage, message = progress_events.get_nowait()
            report(progress, stage, message)
        corrected_transcript = correction_future.result()
        corrected_transcript = sanitize_transcript_text(corrected_transcript)
        figures = figures_future.result()

    if not correction_cache_valid:
        corrected_path.write_text(corrected_transcript, encoding="utf-8")
        cache["transcript_correction"] = {
            "pdf_hash": pdf_hash,
            "transcript_hash": transcript_hash,
            "version": TRANSCRIPT_CACHE_VERSION,
        }
    update_corrected_transcript(lecture_id, str(corrected_path))

    if not figure_cache_valid:
        replace_lecture_figures(lecture_id, figures)
        cache["figures"] = {
            "pdf_hash": pdf_hash,
            "version": FIGURE_CACHE_VERSION,
        }
    _save_cache_manifest(cache_path, cache)

    figure_page_numbers = sorted({
        figure["page_no"]
        for figure in figures
    })
    update_summary_image_pages(lecture_id, figure_page_numbers)

    # 정리본에는 강의록 자체에 포함된 기출만 사용한다. 과목의 `기출`
    # 폴더 자료는 DB에 보존하되 정리 요청·이미지 추출에는 전달하지 않는다.
    exam_files = []
    cached_exam_figures = _rows_to_dicts(get_lecture_exam_figures(lecture_id))
    cached_exam_figures_are_lecture_only = all(
        figure.get("source_kind") == "lecture"
        and Path(figure.get("source_path") or "") == Path(pdf_path)
        for figure in cached_exam_figures
    )
    exam_figure_cache_valid = (
        cache.get("exam_figures") == {
            "pdf_hash": pdf_hash,
            "source": "lecture-only",
            "version": EXAM_FIGURE_CACHE_VERSION,
        }
        and (
            not cached_exam_figures
            or _all_paths_exist(cached_exam_figures, "image_path")
        )
        and cached_exam_figures_are_lecture_only
    )
    if exam_figure_cache_valid:
        exam_figures = cached_exam_figures
    else:
        report(0.70, "3/6", "강의록 안의 기출 이미지를 정리하고 있습니다.")
        exam_figures = prepare_exam_figures(
            lecture_id,
            pdf_path,
            page_records,
            exam_files,
            lecture_dir / "exam_figures",
        )
        replace_lecture_exam_figures(lecture_id, exam_figures)
        cache["exam_figures"] = {
            "pdf_hash": pdf_hash,
            "source": "lecture-only",
            "version": EXAM_FIGURE_CACHE_VERSION,
        }
        _save_cache_manifest(cache_path, cache)

    report(0.72, "4/6", "AI 정리 요청을 구성하고 있습니다.")
    summary_started = monotonic()

    # 정리본 본문(1·2·3·5·6·7번)과 기출 분석(4번)을 완전히 분리된 Codex
    # 호출로 만든다. 예전에는 한 호출에 본문과(기출이 많으면) 수십 개
    # 문항 분석을 한꺼번에 요청해서, AI가 답변 분량을 나눠 쓰며 본문
    # 설명이 압축되는 문제가 있었다. 본문 호출에는 기출 이미지를 아예
    # 첨부하지 않아 본문 깊이에만 집중하게 하고, 기출 하이라이트는
    # 두 결과를 합친 뒤 `highlight_summary_with_codex`가 근거를 보고
    # 정확히 표시한다.
    main_request_content = build_chatgpt_request(
        subject_name=subject_name,
        lecture_no=lecture_no,
        lecture_title=lecture_title,
        lecture_pages=page_records,
        transcript_text=corrected_transcript,
        selected_image_pages=figure_page_numbers,
        figures=figures,
    )
    main_image_paths = [
        figure["image_path"]
        for figure in figures
        if Path(figure["image_path"]).exists()
    ]
    page_image_by_no = {
        page["page_no"]: page["image_path"]
        for page in page_records
        if page.get("image_path")
    }
    EXAM_ANALYSIS_BATCH_SIZE = 6

    def _exam_batch_image_paths(batch):
        # 문항 크롭 이미지에는 연도·중간/기말 표시가 없다(강의록 페이지
        # 위에 별도 텍스트로만 있음). 이미지만 보내면 AI가 그 표시를
        # 못 읽어 "연도·시험 구분 미상"으로 쓰게 되므로, 해당 문항이
        # 있는 강의록 원본 페이지 이미지도 함께 보낸다.
        paths = []
        seen = set()
        for figure in batch:
            for path in (
                page_image_by_no.get(figure.get("page_no")),
                figure["image_path"],
            ):
                if path and path not in seen and Path(path).exists():
                    paths.append(path)
                    seen.add(path)
        return paths

    # 정리본을 만드는 실제 입력(본문 요청 + 기출 문항 목록 + 첨부 이미지)이
    # 지난 실행과 같으면 Codex를 다시 호출하지 않는다.
    summary_signature = hashlib.sha256(
        "\n".join([
            main_request_content,
            *sorted(main_image_paths),
            *sorted(figure["figure_key"] for figure in exam_figures),
        ]).encode("utf-8")
    ).hexdigest()
    existing_summary = get_lecture_summary(lecture_id)
    summary_cache_valid = (
        existing_summary is not None
        and cache.get("summary") == {
            "signature": summary_signature,
            "version": SUMMARY_CACHE_VERSION,
        }
    )

    if summary_cache_valid:
        report(0.94, "4/6", "입력이 그대로라 기존 AI 정리본을 재사용했습니다.")
        summary = existing_summary["content"]
        timings["최종 정리"] = monotonic() - summary_started
    else:
        def update_summary_progress(elapsed_seconds):
            minutes, seconds = divmod(elapsed_seconds, 60)
            report(
                min(0.88, 0.75 + elapsed_seconds / 900 * 0.13),
                "4/6",
                (
                    "AI 정리본 본문 생성 중 · "
                    f"경과 시간 {minutes:02d}:{seconds:02d}"
                ),
            )

        summary = generate_summary_with_codex(
            codex_path,
            main_request_content,
            image_paths=main_image_paths,
            progress_callback=update_summary_progress,
        )
        model_label = f"Codex - {CODEX_MODEL} ({CODEX_REASONING_EFFORT})"
        summary = sanitize_generated_summary(summary, exam_figures)

        # 강의록에서 확인된 기출 문항 전체를 배치로 나눠 분석을 요청한다
        # (그림 탐지와 같은 이유: 첨부 이미지가 너무 많으면 응답 품질이
        # 떨어진다). 배치 결과를 모아 문항별 상세 분석으로 합친다. 이
        # 섹션은 번호 없는 `## 기출문제 상세 분석`으로 두고 별도 "기출
        # 분석" 탭·PDF로만 보여준다. 정리본 본문에는 대신 아래에서 생성할
        # 간략한 `## 4. 자주 출제되는 내용` 요약만 남긴다.
        if exam_figures:
            exam_sections = []
            for batch_start in range(0, len(exam_figures), EXAM_ANALYSIS_BATCH_SIZE):
                batch = exam_figures[batch_start:batch_start + EXAM_ANALYSIS_BATCH_SIZE]
                report(
                    0.90,
                    "4/6",
                    "기출 문항을 분석하고 있습니다 · "
                    f"{batch_start + len(batch)}/{len(exam_figures)}",
                )
                batch_request = build_exam_analysis_request(
                    subject_name, lecture_title, batch
                )
                batch_content = generate_summary_with_codex(
                    codex_path,
                    batch_request,
                    image_paths=_exam_batch_image_paths(batch),
                    timeout_seconds=600,
                )
                exam_sections.append(batch_content)

            exam_analysis_content = (
                "## 기출문제 상세 분석\n\n" + "\n\n".join(exam_sections)
            )
            summary = insert_missing_exam_sections(summary, exam_analysis_content)

            # 배치 하나가 문항을 빠뜨렸을 수 있으니, 빠진 것만 골라 한 번
            # 더 보충 요청한다. 이미 다 실렸으면 추가 호출 없이 지나간다.
            missing_figures = find_missing_exam_figures(summary, exam_figures)
            if missing_figures:
                for batch_start in range(0, len(missing_figures), EXAM_ANALYSIS_BATCH_SIZE):
                    batch = missing_figures[batch_start:batch_start + EXAM_ANALYSIS_BATCH_SIZE]
                    report(
                        0.935,
                        "4/6",
                        f"빠진 기출 문항을 보충하고 있습니다 · "
                        f"{batch_start + len(batch)}/{len(missing_figures)}",
                    )
                    fill_request = build_exam_analysis_request(
                        subject_name, lecture_title, batch
                    )
                    fill_content = generate_summary_with_codex(
                        codex_path,
                        fill_request,
                        image_paths=_exam_batch_image_paths(batch),
                        timeout_seconds=600,
                    )
                    summary = insert_missing_exam_sections(summary, fill_content)

            summary = sanitize_generated_summary(summary, exam_figures)

            # 상세 분석은 별도 탭으로 빠지지만, 정리본 본문에도 "이 강의는
            # 무엇이 자주 나오는가"를 한눈에 볼 수 있는 간략한 `## 4.`
            # 요약을 남긴다. 문항별 상세 분석을 다시 읽어 반복 패턴만
            # 간추리므로 이미지 없이 텍스트만으로 충분하다.
            report(0.935, "4/6", "자주 출제되는 내용을 요약하고 있습니다.")
            try:
                _, detailed_exam_content = split_summary_sections(summary)
                frequent_topics_request = build_frequent_exam_topics_request(
                    subject_name, lecture_title, detailed_exam_content
                )
                frequent_topics_content = generate_summary_with_codex(
                    codex_path,
                    frequent_topics_request,
                    image_paths=[],
                    timeout_seconds=600,
                )
                summary = insert_frequent_topics_section(
                    summary, frequent_topics_content
                )
                summary = sanitize_generated_summary(summary, exam_figures)
            except Exception as error:
                # 이 요약도 부가 기능이라, 실패해도 정리본 생성 자체를
                # 실패로 만들지 않는다(본문에 4번이 비어 있을 뿐 상세
                # 분석은 그대로 남는다). 다만 조용히 넘어가면 나중에
                # "4번이 왜 비었지" 하고 원인을 못 찾게 되므로, 최소한
                # 로그에는 남긴다(effort를 medium으로 올린 뒤 이 단계가
                # 타임아웃으로 조용히 실패해 하이라이트가 통째로 빠지는
                # 것을 겪은 적이 있다).
                print(
                    f"[자주 출제되는 내용 요약 실패] {type(error).__name__}: {error}",
                    flush=True,
                )
                warnings.append("자주 출제되는 내용 요약 실패")
                report(
                    0.935,
                    "4/6",
                    f"빈출 요약 생성에 실패해 건너뜁니다: {error}",
                )

            # 본문과 기출 분석을 별도로 생성해 본문 쪽에는 EXAM_HIGHLIGHT를
            # 표시하지 않았다. 이제 둘 다 갖춰졌으니, 기출 분석과 실제로
            # 일치하는 본문 문장에만 정확히 하이라이트를 붙인다.
            report(0.94, "4/6", "기출과 겹치는 본문 문장을 표시하고 있습니다.")
            try:
                summary, _ = highlight_summary_with_codex(codex_path, summary)
            except Exception as error:
                # 하이라이트도 부가 기능이므로 실패해도 정리본 생성 자체를
                # 실패로 만들지 않지만, 위와 같은 이유로 로그에는 남긴다.
                print(
                    f"[기출 하이라이트 실패] {type(error).__name__}: {error}",
                    flush=True,
                )
                warnings.append("기출 하이라이트 적용 실패")
                report(
                    0.94,
                    "4/6",
                    f"기출 하이라이트 적용에 실패해 건너뜁니다: {error}",
                )

        timings["최종 정리"] = monotonic() - summary_started

        report(0.94, "5/6", "완성된 AI 정리본을 저장하고 있습니다.")
        save_lecture_ai_results(
            lecture_id,
            [],
            summary,
            model_label,
        )
        cache["summary"] = {
            "signature": summary_signature,
            "version": SUMMARY_CACHE_VERSION,
        }
        _save_cache_manifest(cache_path, cache)

    quiz_started = monotonic()
    if summary_cache_valid and get_base_web_quiz(lecture_id) is not None:
        report(0.95, "6/6", "정리본이 그대로라 기존 복습 퀴즈를 재사용했습니다.")
    else:
        report(0.95, "6/6", "웹용 4지선다 복습 문제 10개를 만들고 있습니다.")
        # 정리본은 항상 한국어로 번역되므로, 실제 시험 언어(전체 영어 여부,
        # 혹은 한국어 문장에 영어 용어가 섞이는 방식)는 강의록에서 크롭한
        # 실제 기출 문항 원문에서만 확인할 수 있다.
        exam_texts = [figure.get("extracted_text", "") for figure in exam_figures]
        web_quiz = generate_base_multiple_choice_quiz(
            codex_path, summary, exam_texts=exam_texts
        )
        save_base_web_quiz(
            lecture_id,
            web_quiz,
            "Codex - 자동 객관식 생성",
        )
    timings["웹 객관식"] = monotonic() - quiz_started
    timings["전체"] = monotonic() - automation_started
    timing_text = " · ".join(
        f"{label} {seconds / 60:.1f}분"
        for label, seconds in timings.items()
    )
    report(1.0, "완료", f"전체 자동 처리가 끝났습니다. {timing_text}")

    return {
        "page_count": len(page_records),
        "figure_count": len(figures),
        "corrected_transcript_path": str(corrected_path),
        "timing_text": timing_text,
        "warnings": warnings,
    }
