import hashlib
import json
import re
import shutil
import unicodedata
from functools import lru_cache
from pathlib import Path

import pymupdf

from database import (
    add_lecture,
    add_subject,
    get_lecture_summary,
    get_lectures,
    get_subjects,
    get_subject_exam_files,
    upsert_subject_exam_file,
    update_corrected_transcript,
    update_lecture_pdf,
    update_lecture_title,
    update_lecture_transcript,
)


SEMESTER_PATTERN = re.compile(r"^\d+_\d{2}-[12]$")
LECTURE_NUMBER_PATTERN = re.compile(r"(?:제\s*)?(\d{1,2})\s*강")
WEEK_NUMBER_PATTERN = re.compile(r"(?:제\s*)?(\d{1,2})\s*주차")
LEADING_NUMBER_PATTERN = re.compile(r"^(\d{1,2})(?:[._-]\d+)?(?:[_\s-]|$)")
NON_LECTURE_KEYWORDS = ("계획서", "강의계획", "시간표", "syllabus")


def _normalized(text):
    return unicodedata.normalize("NFC", text)


def discover_google_drive_semesters(cloud_storage=None):
    """Mac에 연결된 Google Drive에서 KHU 학기 폴더를 자동으로 찾는다."""
    cloud_storage = Path(
        cloud_storage
        or Path.home() / "Library" / "CloudStorage"
    )
    semesters = []

    for drive_root in cloud_storage.glob("GoogleDrive-*/My Drive/KHU"):
        if not drive_root.is_dir():
            continue
        for child in drive_root.iterdir():
            if child.is_dir() and SEMESTER_PATTERN.match(_normalized(child.name)):
                semesters.append(child)

    return sorted(
        set(semesters),
        key=lambda path: (_normalized(path.name), str(path)),
        reverse=True,
    )


def _find_named_child(parent, expected_name):
    for child in parent.iterdir():
        if child.is_dir() and _normalized(child.name) == expected_name:
            return child
    return None


def _subject_name(course_dir):
    name = re.sub(r"^\d+[_\s-]+", "", _normalized(course_dir.name)).strip()
    return name or _normalized(course_dir.name)


def _lecture_number(path):
    name = _normalized(path.stem)
    explicit = LECTURE_NUMBER_PATTERN.search(name)
    if explicit:
        return int(explicit.group(1))
    week = WEEK_NUMBER_PATTERN.search(name)
    if week:
        return int(week.group(1))
    leading = LEADING_NUMBER_PATTERN.match(name)
    if leading:
        return int(leading.group(1))
    return None


def _is_non_lecture_document(path, lecture_no):
    normalized_name = _normalized(path.stem).lower()
    return (
        lecture_no <= 0
        or any(keyword in normalized_name for keyword in NON_LECTURE_KEYWORDS)
    )


def _lecture_title(path, lecture_no):
    title = _normalized(path.stem)
    title = re.sub(r"^\d{1,2}(?:[._-]\d+)?[_\s-]*", "", title)
    title = re.sub(rf"(?:제\s*)?{lecture_no}\s*강[_\s-]*", "", title)
    title = re.sub(r"[_\s-]*\d{6,8}$", "", title)
    return title.replace("_", " ").strip(" -") or f"{lecture_no}강"


def _clean_pdf_title(text):
    return " ".join(_normalized(text).replace("\x00", " ").split()).strip(" -–—:|")


def _valid_pdf_title(text):
    compact = "".join(text.split())
    return (
        4 <= len(compact) <= 140
        and sum(character.isalnum() for character in compact) >= 4
        and not re.fullmatch(r"[\d.\-/ ]+", text)
    )


@lru_cache(maxsize=256)
def _extract_pdf_lecture_title_cached(
    pdf_path, lecture_no, modified_ns, file_size, tessdata_dir
):
    """표지에서 `1. 강의 제목` 형태를 찾아 강의명을 반환한다."""
    try:
        with pymupdf.open(pdf_path) as document:
            if not document:
                return None
            page = document[0]
            text = page.get_text("text").strip()
            if len("".join(text.split())) < 20:
                tessdata_dir = Path(tessdata_dir)
                textpage = page.get_textpage_ocr(
                    language="kor+eng",
                    dpi=150,
                    full=True,
                    tessdata=str(tessdata_dir),
                )
                text = page.get_text("text", textpage=textpage).strip()
    except (OSError, RuntimeError, ValueError):
        return None

    lines = [_clean_pdf_title(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    number = re.escape(str(lecture_no))
    inline_pattern = re.compile(
        rf"^(?:lecture\s*)?{number}\s*[.):：-]\s*(.+)$",
        re.IGNORECASE,
    )
    marker_pattern = re.compile(
        rf"^(?:lecture\s*)?{number}\s*[.):：-]?$",
        re.IGNORECASE,
    )

    for index, line in enumerate(lines):
        inline = inline_pattern.match(line)
        if inline:
            title = _clean_pdf_title(inline.group(1))
            if _valid_pdf_title(title):
                return title
        if marker_pattern.match(line):
            for following in lines[index + 1:index + 4]:
                if _valid_pdf_title(following):
                    return following
    # 번호가 없는 표지도 첫 줄에 강의명을 크게 적는 경우가 많다.
    # `사람생식`처럼 짧고 명확한 첫 줄은 파일명보다 우선하되, 학교명·교수명·
    # 학습목표 같은 표지 메타데이터는 제목으로 선택하지 않는다.
    metadata_pattern = re.compile(
        r"(?:교수|professor|대학교|대학|의과대학|학습\s*목표|"
        r"department|faculty|20\d{2}[./-]\d{1,2})",
        re.IGNORECASE,
    )
    for line in lines[:5]:
        if (
            _valid_pdf_title(line)
            and len("".join(line.split())) <= 60
            and not metadata_pattern.search(line)
        ):
            return line
    return None


def _extract_pdf_lecture_title(pdf_path, lecture_no, tessdata_dir=None):
    """파일이 바뀌지 않은 동안에는 표지 OCR 제목을 재사용한다."""
    pdf_path = Path(pdf_path)
    try:
        stat = pdf_path.stat()
    except OSError:
        return None
    tessdata_dir = Path(tessdata_dir or Path(__file__).parent / "ocr_data")
    return _extract_pdf_lecture_title_cached(
        str(pdf_path), lecture_no, stat.st_mtime_ns, stat.st_size, str(tessdata_dir)
    )


def _looks_automatic_title(title, subject_name, lecture_no):
    """사용자가 직접 지은 제목이 아니라 파일명 기반 제목인지 보수적으로 판별한다."""
    normalized = _normalized(title or "").lower()
    subject = _normalized(subject_name).lower()
    return bool(
        not normalized
        or normalized == f"{lecture_no}강"
        or subject in normalized
        or re.search(rf"(?:{lecture_no}\s*(?:주차|강))", normalized)
    )


def scan_semester_folder(semester_path):
    """과목별 강의록 PDF와 TXT 전사문을 강의 번호로 연결한다."""
    semester_path = Path(semester_path).expanduser()
    if not semester_path.is_dir():
        raise ValueError("선택한 Google Drive 학기 폴더를 찾을 수 없습니다.")

    candidates = []
    for course_dir in sorted(
        (path for path in semester_path.iterdir() if path.is_dir()),
        key=lambda path: _normalized(path.name),
    ):
        pdf_dir = _find_named_child(course_dir, "강의록")
        transcript_dir = _find_named_child(course_dir, "전사문")
        exam_dir = _find_named_child(course_dir, "기출")
        if not pdf_dir and not transcript_dir and not exam_dir:
            continue

        if exam_dir:
            for exam_path in sorted(exam_dir.rglob("*.pdf")):
                if exam_path.is_file():
                    candidates.append(
                        {
                            "item_type": "exam",
                            "subject": _subject_name(course_dir),
                            "file_name": _normalized(exam_path.name),
                            "exam_relative_path": str(exam_path.relative_to(exam_dir)),
                            "exam_path": str(exam_path),
                            "status": "기출 동기화 가능",
                        }
                    )

        grouped = {}
        for kind, folder, suffixes in (
            ("pdf", pdf_dir, {".pdf"}),
            ("transcript", transcript_dir, {".txt"}),
        ):
            if not folder:
                continue
            # 과목 폴더 안에서 주차별 하위 폴더를 사용해도 찾을 수 있게 한다.
            for path in folder.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in suffixes:
                    continue
                lecture_no = _lecture_number(path)
                if lecture_no is None:
                    continue
                if kind == "pdf" and _is_non_lecture_document(path, lecture_no):
                    continue
                grouped.setdefault(
                    lecture_no, {"pdf": [], "transcript": []}
                )[
                    kind
                ].append(path)

        for lecture_no, files in sorted(grouped.items()):
            pdf_files = sorted(files["pdf"])
            transcript_files = sorted(files["transcript"])
            if pdf_files and transcript_files:
                status = "동기화 가능"
            elif not pdf_files:
                status = "전사문만 동기화"
            else:
                status = "강의록만 동기화"

            title_source = pdf_files[0] if pdf_files else (
                transcript_files[0] if transcript_files else None
            )
            file_title = (
                _lecture_title(title_source, lecture_no)
                if title_source else f"{lecture_no}강"
            )
            pdf_title = (
                _extract_pdf_lecture_title(pdf_files[0], lecture_no)
                if pdf_files else None
            )
            candidates.append(
                {
                    "item_type": "lecture",
                    "subject": _subject_name(course_dir),
                    "lecture_no": lecture_no,
                    "title": pdf_title or file_title,
                    "title_source": "강의록 표지" if pdf_title else "파일명",
                    "pdf_path": str(pdf_files[0]) if len(pdf_files) == 1 else None,
                    "pdf_paths": [str(path) for path in pdf_files],
                    "transcript_path": (
                        str(transcript_files[0])
                        if len(transcript_files) == 1
                        else None
                    ),
                    "transcript_paths": [str(path) for path in transcript_files],
                    "pdf_count": len(pdf_files),
                    "transcript_count": len(transcript_files),
                    "status": status,
                }
            )

    return candidates


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_if_changed(source, destination):
    source = Path(source)
    destination = Path(destination)
    if destination.exists() and _sha256(source) == _sha256(destination):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def _candidate_sources(candidate, kind):
    paths = candidate.get(f"{kind}_paths")
    if paths is None:
        single = candidate.get(f"{kind}_path")
        paths = [single] if single else []
    return [Path(path) for path in paths]


def _source_fingerprint(paths):
    return [
        {"path": str(path), "sha256": _sha256(path)}
        for path in paths
    ]


def _load_bundle_manifest(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _bundle_changed(paths, destination, manifest_path, key):
    if not paths or not destination.exists():
        return bool(paths)
    # 기존 단일 파일은 manifest가 없어도 파일 내용으로 바로 비교한다.
    if len(paths) == 1 and _sha256(paths[0]) == _sha256(destination):
        return False
    manifest = _load_bundle_manifest(manifest_path)
    return manifest.get(key) != _source_fingerprint(paths)


def _save_bundle_manifest(manifest_path, key, paths):
    manifest = _load_bundle_manifest(manifest_path)
    manifest[key] = _source_fingerprint(paths)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(manifest_path)


def _sync_pdf_bundle(paths, destination, manifest_path):
    if not paths:
        return False
    changed = _bundle_changed(paths, destination, manifest_path, "pdf")
    if not changed:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if len(paths) == 1:
        shutil.copy2(paths[0], destination)
    else:
        combined = pymupdf.open()
        try:
            for source_path in paths:
                with pymupdf.open(source_path) as source_document:
                    combined.insert_pdf(source_document)
            temporary = destination.with_suffix(".tmp.pdf")
            combined.save(temporary, garbage=4, deflate=True)
            temporary.replace(destination)
        finally:
            combined.close()
    _save_bundle_manifest(manifest_path, "pdf", paths)
    return True


def _sync_transcript_bundle(paths, destination, manifest_path):
    if not paths:
        return False
    changed = _bundle_changed(paths, destination, manifest_path, "transcript")
    if not changed:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if len(paths) == 1:
        shutil.copy2(paths[0], destination)
    else:
        sections = []
        for index, source_path in enumerate(paths, start=1):
            content = source_path.read_text(encoding="utf-8-sig").strip()
            sections.append(
                f"===== 전사문 {index}: {source_path.name} =====\n{content}"
            )
        temporary = destination.with_suffix(".tmp")
        temporary.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
        temporary.replace(destination)
    _save_bundle_manifest(manifest_path, "transcript", paths)
    return True


def filter_actionable_candidates(candidates, data_dir):
    """새 자료·변경 자료·정리본 미생성 자료만 화면에 남긴다."""
    data_dir = Path(data_dir)
    subjects = {
        _normalized(row["name"]): row
        for row in get_subjects()
    }
    actionable = []
    hidden_unchanged = 0
    hidden_incomplete = 0

    for candidate in candidates:
        if candidate.get("item_type") == "exam":
            subject = subjects.get(_normalized(candidate["subject"]))
            source = Path(candidate["exam_path"])
            existing = [] if subject is None else get_subject_exam_files(subject["id"])
            saved = next(
                (row for row in existing if row["source_path"] == str(source)),
                None,
            )
            if saved is None:
                actionable.append({**candidate, "change_reason": "새 기출"})
            elif not Path(saved["local_path"]).exists() or saved["file_hash"] != _sha256(source):
                actionable.append({**candidate, "change_reason": "기출 변경"})
            else:
                hidden_unchanged += 1
            continue
        subject = subjects.get(_normalized(candidate["subject"]))
        lecture = None
        if subject:
            lecture = next(
                (
                    row
                    for row in get_lectures(subject["id"])
                    if row["lecture_no"] == candidate["lecture_no"]
                ),
                None,
            )

        reason = None
        if lecture is None:
            if candidate["status"] == "동기화 가능":
                reason = "새 강의"
            else:
                reason = f'새 강의 · {candidate["status"]}'
        else:
            lecture_dir = (
                data_dir
                / subject["name"]
                / f'{candidate["lecture_no"]:02d}'
            )
            local_pdf = Path(lecture["pdf_path"] or lecture_dir / "lecture.pdf")
            local_transcript = Path(
                lecture["transcript_path"] or lecture_dir / "transcript.txt"
            )
            manifest_path = lecture_dir / "source_bundle.json"
            pdf_sources = _candidate_sources(candidate, "pdf")
            transcript_sources = _candidate_sources(candidate, "transcript")
            if (
                candidate.get("title_source") == "강의록 표지"
                and candidate["title"] != lecture["title"]
                and _looks_automatic_title(
                    lecture["title"], subject["name"], candidate["lecture_no"]
                )
            ):
                reason = "강의 제목 개선"
            elif _bundle_changed(
                pdf_sources, local_pdf, manifest_path, "pdf"
            ):
                reason = "강의록 변경"
            elif _bundle_changed(
                transcript_sources,
                local_transcript,
                manifest_path,
                "transcript",
            ):
                reason = "전사문 변경"
            elif (
                local_pdf.exists()
                and local_transcript.exists()
                and get_lecture_summary(lecture["id"]) is None
            ):
                reason = "정리본 없음"
            else:
                state_path = lecture_dir / "generation_state.txt"
                if (
                    state_path.exists()
                    and state_path.read_text(encoding="utf-8").strip() == "pending"
                ):
                    reason = "생성 대기"

        if reason:
            actionable.append({**candidate, "change_reason": reason})
        else:
            hidden_unchanged += 1

    return actionable, {
        "hidden_unchanged": hidden_unchanged,
        "hidden_incomplete": hidden_incomplete,
    }


def sync_candidates(candidates, data_dir):
    """주차가 확인된 자료를 복사하고, 한쪽만 있어도 강의를 먼저 만든다."""
    data_dir = Path(data_dir).expanduser().resolve()
    results = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "generation_jobs": [],
        "exam_created": 0,
        "exam_updated": 0,
    }

    for candidate in candidates:
        if candidate.get("item_type") == "exam":
            subjects = {_normalized(row["name"]): row for row in get_subjects()}
            subject = subjects.get(_normalized(candidate["subject"]))
            if subject is None:
                add_subject(candidate["subject"])
                subject = {
                    _normalized(row["name"]): row for row in get_subjects()
                }[_normalized(candidate["subject"])]
            source = Path(candidate["exam_path"])
            existing = next(
                (
                    row for row in get_subject_exam_files(subject["id"])
                    if row["source_path"] == str(source)
                ),
                None,
            )
            relative_path = Path(
                candidate.get("exam_relative_path") or source.name
            )
            destination = data_dir / subject["name"] / "기출" / relative_path
            changed = _copy_if_changed(source, destination)
            file_hash = _sha256(source)
            if existing is None or changed or existing["file_hash"] != file_hash:
                extracted_text = _extract_exam_pdf_text(source)
                upsert_subject_exam_file(
                    subject["id"], str(source), str(destination), source.name,
                    file_hash, extracted_text,
                )
                results["exam_created" if existing is None else "exam_updated"] += 1
            else:
                results["unchanged"] += 1
            continue
        subjects = {
            _normalized(row["name"]): row
            for row in get_subjects()
        }
        subject = subjects.get(_normalized(candidate["subject"]))
        if subject is None:
            add_subject(candidate["subject"])
            subjects = {
                _normalized(row["name"]): row
                for row in get_subjects()
            }
            subject = subjects[_normalized(candidate["subject"])]

        lectures = {
            row["lecture_no"]: row
            for row in get_lectures(subject["id"])
        }
        lecture = lectures.get(candidate["lecture_no"])
        was_created = lecture is None
        if was_created:
            add_lecture(
                subject["id"],
                candidate["lecture_no"],
                candidate["title"],
            )
            lecture = {
                row["lecture_no"]: row
                for row in get_lectures(subject["id"])
            }[candidate["lecture_no"]]
        elif (
            candidate.get("title_source") == "강의록 표지"
            and candidate["title"] != lecture["title"]
            and _looks_automatic_title(
                lecture["title"], subject["name"], candidate["lecture_no"]
            )
        ):
            update_lecture_title(lecture["id"], candidate["title"])
            lecture = dict(lecture)
            lecture["title"] = candidate["title"]

        lecture_dir = (
            data_dir
            / subject["name"]
            / f'{candidate["lecture_no"]:02d}'
        )
        pdf_destination = lecture_dir / "lecture.pdf"
        transcript_destination = lecture_dir / "transcript.txt"
        manifest_path = lecture_dir / "source_bundle.json"
        pdf_sources = _candidate_sources(candidate, "pdf")
        transcript_sources = _candidate_sources(candidate, "transcript")
        pdf_changed = _sync_pdf_bundle(
            pdf_sources, pdf_destination, manifest_path
        )
        transcript_changed = _sync_transcript_bundle(
            transcript_sources, transcript_destination, manifest_path
        )
        if pdf_sources:
            update_lecture_pdf(lecture["id"], str(pdf_destination))
        if transcript_sources:
            update_lecture_transcript(
                lecture["id"], str(transcript_destination)
            )

        if pdf_changed or transcript_changed:
            # 원본이 바뀌었는데 이전 교정본을 사용하는 일을 막는다.
            update_corrected_transcript(lecture["id"], None)

        generation_state_path = lecture_dir / "generation_state.txt"
        saved_summary = get_lecture_summary(lecture["id"])
        if (
            was_created or pdf_changed or transcript_changed
            or saved_summary is None
        ):
            generation_state_path.write_text("pending", encoding="utf-8")

        # 한쪽 파일만 먼저 들어온 경우에는 강의와 파일 연결까지만 한다.
        # 나머지 자료가 다음 동기화에서 들어오면 그때 자동 생성한다.
        complete_materials = (
            pdf_destination.exists()
            and transcript_destination.exists()
        )
        generation_required = complete_materials and (
            generation_state_path.exists()
            and generation_state_path.read_text(encoding="utf-8").strip()
            == "pending"
        )
        if generation_required:
            results["generation_jobs"].append(
                {
                    "lecture_id": lecture["id"],
                    "subject_name": subject["name"],
                    "lecture_no": candidate["lecture_no"],
                    "lecture_title": lecture["title"] or candidate["title"],
                    "pdf_path": str(pdf_destination),
                    "transcript_path": (
                        str(transcript_destination)
                        if transcript_destination.exists() else None
                    ),
                    "generation_state_path": str(generation_state_path),
                }
            )

        if was_created:
            results["created"] += 1
        elif pdf_changed or transcript_changed:
            results["updated"] += 1
        else:
            results["unchanged"] += 1

    return results


def _extract_exam_pdf_text(pdf_path):
    """기출 PDF의 텍스트를 추출하고 스캔 페이지는 OCR로 보완한다."""
    sections = []
    tessdata = Path(__file__).resolve().parent / "ocr_data"
    with pymupdf.open(pdf_path) as document:
        for page_no, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            compact = "".join(text.split())
            readable = sum(ch.isalnum() or "가" <= ch <= "힣" for ch in compact)
            usable = len(compact) >= 40 and readable / max(1, len(compact)) >= 0.6
            if not usable:
                try:
                    textpage = page.get_textpage_ocr(
                        language="kor+eng", dpi=150, full=True,
                        tessdata=str(tessdata),
                    )
                    text = page.get_text("text", textpage=textpage).strip()
                except Exception:
                    pass
            if text:
                sections.append(f"[기출 PDF {page_no}페이지]\n{text}")
    return "\n\n".join(sections)
