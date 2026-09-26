"""Read colony cage cards with OCR running on the application server."""

from __future__ import annotations

import re
from contextlib import suppress
from datetime import datetime
from threading import Lock
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.card_ocr import read_photo_lines
from app.photo_storage import PhotoStore

MAX_PHOTO_BYTES = 20 * 1024 * 1024
router = APIRouter()
_ocr_lock = Lock()
_DATE = re.compile(r"\b(\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))\b")


def _center(line: dict[str, Any]) -> tuple[float, float]:
    points = line["box"]
    return sum(p[0] for p in points) / 4, sum(p[1] for p in points) / 4


def parse_card_lines(lines: list[dict[str, Any]]) -> dict[str, Any]:
    cage_ids = set()
    for item in lines:
        cage_ids.update(re.findall(r"\bCC\d{8}\b", item["text"].upper()))
    result: dict[str, Any] = {
        "cage_card_id": next(iter(cage_ids)) if len(cage_ids) == 1 else None,
        "line": None,
        "rows": [],
    }
    for item in lines:
        if re.search(r"\bline\s+\d+\b", item["text"], re.IGNORECASE):
            y = _center(item)[1]
            height = max(p[1] for p in item["box"]) - min(p[1] for p in item["box"])
            result["line"] = " ".join(
                part["text"]
                for part in sorted(lines, key=lambda part: _center(part)[0])
                if abs(_center(part)[1] - y) < height / 2
            )
            break
    id_headers = [item for item in lines if item["text"].strip().upper() == "ID"]
    genotype_headers = [item for item in lines if item["text"].strip().lower() == "genotype"]
    if len(id_headers) != 1 or len(genotype_headers) != 1:
        return result
    id_header, genotype_header = id_headers[0], genotype_headers[0]
    header_x, header_y = _center(id_header)
    header_height = max(p[1] for p in id_header["box"]) - min(p[1] for p in id_header["box"])
    genotype_x = min(p[0] for p in genotype_header["box"])
    ids = sorted(
        (
            item
            for item in lines
            if re.fullmatch(r"\d{3,10}", item["text"].strip())
            and item["confidence"] >= 0.8
            and abs(min(p[0] for p in item["box"]) - header_x) < header_height * 2
            and _center(item)[1] > header_y + header_height / 2
        ),
        key=lambda item: _center(item)[1],
    )
    for index, item in enumerate(ids):
        x, y = _center(item)
        height = max(p[1] for p in item["box"]) - min(p[1] for p in item["box"])
        neighboring_gaps = [
            abs(_center(ids[other])[1] - y)
            for other in (index - 1, index + 1)
            if 0 <= other < len(ids)
        ]
        tolerance = min(neighboring_gaps) * 0.48 if neighboring_gaps else height * 0.65
        cells = sorted(
            (
                part
                for part in lines
                if _center(part)[0] > x and abs(_center(part)[1] - y) <= tolerance
            ),
            key=lambda part: _center(part)[0],
        )
        metadata = " ".join(part["text"] for part in cells if _center(part)[0] < genotype_x)
        sex_match = re.search(r"(?:^|\s)([MF])(?=\s|\d|$)", metadata.upper())
        dob = None
        date_match = _DATE.search(metadata)
        if date_match:
            value = date_match[1]
            with suppress(ValueError):
                dob = (
                    datetime.strptime(
                        value, "%m/%d/%Y" if len(value.split("/")[-1]) == 4 else "%m/%d/%y"
                    )
                    .date()
                    .isoformat()
                )
        genotype = " ".join(
            part["text"]
            for part in cells
            if min(p[0] for p in part["box"]) >= genotype_x - header_height / 4
        ).strip()
        genotype = {"十": "+", "一": "-", "−": "-", "—": "-", "–": "-"}.get(genotype, genotype)
        result["rows"].append(
            {
                "mouse_id": item["text"].strip(),
                "sex": sex_match[1] if sex_match else None,
                "dob": dob,
                "genotype": genotype or None,
            }
        )
    return result


def recognize_photo(payload: bytes) -> dict[str, Any]:
    return parse_card_lines(read_photo_lines(payload))


def _store(request: Request) -> PhotoStore:
    return PhotoStore(request.app.state.settings.photo_directory)


def _photo_links(request: Request, photo: dict[str, Any]) -> dict[str, Any]:
    base_path = str(request.scope.get("root_path", "")).rstrip("/")
    cage_id = photo["cage_id"]
    prefix = f"{base_path}/cages/{cage_id}/photos/{photo['id']}"
    return {
        **photo,
        "preview_url": f"{prefix}/preview" if cage_id is not None else None,
        "original_url": f"{prefix}/original" if cage_id is not None else None,
    }


def photos_for_cage(request: Request, cage_id: int) -> list[dict[str, Any]]:
    try:
        return [_photo_links(request, photo) for photo in _store(request).list(cage_id)]
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Photo storage is unavailable.") from exc


def _require_cage(request: Request, cage_id: int) -> dict[str, Any]:
    cage = request.app.state.database.get_cage(cage_id)
    if cage is None:
        raise HTTPException(status_code=404, detail="Cage not found.")
    return cage


@router.get("/cages/{cage_id}/photos")
def list_cage_photos(request: Request, cage_id: int) -> dict[str, Any]:
    _require_cage(request, cage_id)
    return {"photos": photos_for_cage(request, cage_id)}


@router.get("/cages/{cage_id}/photos/{photo_id}/{kind}")
def get_cage_photo(request: Request, cage_id: int, photo_id: str, kind: str) -> FileResponse:
    _require_cage(request, cage_id)
    try:
        path, photo = _store(request).file(cage_id, photo_id, kind)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Photo not found.") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Photo storage is unavailable.") from exc
    if kind == "preview":
        return FileResponse(path, media_type="image/jpeg")
    return FileResponse(path, filename=photo["filename"], media_type="application/octet-stream")


@router.post("/photos/recognize")
def read_cage_photo(
    request: Request,
    photo_file: Annotated[UploadFile, File()],
    cage_id: Annotated[int | None, Form()] = None,
) -> dict[str, Any]:
    if not _ocr_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429, detail="Another photo is being read. Try again shortly."
        )
    try:
        return _archive_and_read(request, photo_file, cage_id)
    finally:
        _ocr_lock.release()


def _archive_and_read(
    request: Request, photo_file: UploadFile, cage_id: int | None
) -> dict[str, Any]:
    if cage_id is not None:
        _require_cage(request, cage_id)
    payload = photo_file.file.read(MAX_PHOTO_BYTES + 1)
    if len(payload) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Photo must be at most 20 MB.")
    store = _store(request)
    try:
        # Archive before OCR so an unreadable card or unavailable model cannot lose the photo.
        photo = store.save(payload, photo_file.filename or "photo")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, ImportError) as exc:
        raise HTTPException(status_code=503, detail="Photo could not be saved.") from exc

    result: dict[str, Any] = {"cage_card_id": None, "line": None, "rows": []}
    recognition_error = None
    try:
        result = recognize_photo(payload)
    except ValueError as exc:
        recognition_error = HTTPException(status_code=422, detail=f"Photo saved. {exc}")
    except (ImportError, RuntimeError):
        recognition_error = HTTPException(
            status_code=503, detail="Photo saved. Recognition is unavailable."
        )

    result["cage_id"] = None
    if result["cage_card_id"]:
        matches = [
            cage
            for cage in request.app.state.database.list_cages(search=result["cage_card_id"])
            if cage["cage_card_id"].casefold() == result["cage_card_id"].casefold()
        ]
        if len(matches) == 1:
            result["cage_id"] = matches[0]["id"]
    # An explicit cage is usable only when OCR has not identified another card.
    target_id = result["cage_id"]
    if target_id is None and not result["cage_card_id"]:
        target_id = cage_id
    if target_id is not None:
        try:
            photo = store.assign(photo, target_id)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="Photo saved, but could not be linked to the cage."
            ) from exc
    if recognition_error is not None:
        raise recognition_error
    try:
        photo = store.save_recognition(photo, result)
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail="Photo saved, but recognition results could not be saved."
        ) from exc
    result["photo"] = _photo_links(request, photo)
    return result
