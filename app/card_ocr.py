"""Read printed cage rows and handwritten genotype marks with local OCR models."""

from __future__ import annotations

import io
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_MARKS = {"+", "-", "十", "一", "−", "—", "–"}


@lru_cache(maxsize=1)
def _engine() -> Any:
    from rapidocr import RapidOCR

    directory = Path(
        os.environ.get("MOUSELINE_OCR_MODELS", "/opt/senzailab/backend/runtime/colony-ocr-models")
    )
    models = {
        "Det.model_path": directory / "PP-OCRv6_det_small.onnx",
        "Rec.model_path": directory / "PP-OCRv6_rec_small.onnx",
        "Cls.model_path": directory / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    }
    if not all(path.is_file() for path in models.values()):
        raise RuntimeError("Photo recognition models are not installed.")
    return RapidOCR(
        params={
            **{key: str(path) for key, path in models.items()},
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.intra_op_num_threads": 1,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
    )


def _bounds(line: dict[str, Any]) -> tuple[float, float, float, float]:
    points = line["box"]
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def _in_row(line: dict[str, Any], row_y: float, height: float) -> bool:
    bounds = _bounds(line)
    return abs((bounds[1] + bounds[3]) / 2 - row_y) < height * 0.5


def _lines(image: Any, offset: tuple[int, int] = (0, 0)) -> list[dict[str, Any]]:
    # RapidOCR keeps call flags on the engine; set them explicitly after cell recognition.
    result = _engine()(image, use_det=True, use_cls=True, use_rec=True)
    if result.txts is None:
        return []
    return [
        {
            "text": text,
            "confidence": float(score),
            "box": [[float(x) + offset[0], float(y) + offset[1]] for x, y in box],
        }
        for text, score, box in zip(result.txts, result.scores, result.boxes, strict=True)
    ]


def _read_mark(image: Any) -> tuple[str, float] | None:
    import cv2
    import numpy as np
    from PIL import Image

    gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    height, width = ink.shape
    rules = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, max(20, int(width * 0.6))), np.uint8))
    ink[cv2.dilate(rules, np.ones((3, 3), np.uint8)) > 0] = 0
    _, labels, components, _ = cv2.connectedComponentsWithStats(ink, 8)
    clean = np.zeros_like(ink)
    for label, (x, y, component_width, component_height, area) in enumerate(
        components[1:], start=1
    ):
        # Table rules and clipped neighboring rows touch the cell boundary. A blank
        # cell must not become a minus merely because its printed border is visible.
        if (
            x <= 0
            or y <= 0
            or x + component_width >= width
            or y + component_height >= height
            or component_width > width * 0.6
            or component_height < max(2, height * 0.05)
            or area < max(9, width * height * 0.001)
        ):
            continue
        clean[labels == label] = 255
    if not np.any(clean):
        return None
    result = _engine()(
        Image.fromarray(255 - clean).convert("RGB"),
        use_det=False,
        use_cls=False,
        use_rec=True,
    )
    if not result.txts or not result.scores:
        return None
    text, confidence = result.txts[0].strip(), float(result.scores[0])
    return (text, confidence) if text in _MARKS and confidence >= 0.6 else None


def _complete_table(image: Any, lines: list[dict[str, Any]]) -> None:
    id_headers = [line for line in lines if line["text"].strip().upper() == "ID"]
    genotype_headers = [line for line in lines if line["text"].strip().lower() == "genotype"]
    if len(id_headers) != 1 or len(genotype_headers) != 1:
        return
    header = _bounds(id_headers[0])
    genotype = _bounds(genotype_headers[0])
    header_height = header[3] - header[1]
    ids = sorted(
        (
            line
            for line in lines
            if re.fullmatch(r"\d{3,10}", line["text"].strip())
            and line["confidence"] >= 0.8
            and abs(_bounds(line)[0] - (header[0] + header[2]) / 2) < header_height * 2
            and _bounds(line)[1] > header[1] + header_height / 2
        ),
        key=lambda line: _bounds(line)[1],
    )
    if not ids:
        return
    left = max(0, int(header[0] - header_height))
    top = int(header[1])
    right = min(image.width, int(genotype[2] + genotype[2] - genotype[0]))
    bottom = min(image.height, int(max(_bounds(line)[3] for line in ids) + header_height / 4))
    table_lines: list[dict[str, Any]] | None = None
    for line in ids:
        bounds = _bounds(line)
        height = bounds[3] - bounds[1]
        row_y = (bounds[1] + bounds[3]) / 2

        # Keep the first pass's correctly read IDs, dates, and headers. The larger
        # table pass supplies only a missing sex field, so it cannot duplicate rows.
        has_sex = any(
            part["text"].strip() in {"M", "F"} and _in_row(part, row_y, height) for part in lines
        )
        if not has_sex:
            if table_lines is None:
                table_lines = _lines(image.crop((left, top, right, bottom)), (left, top))
            candidates = [
                part
                for part in table_lines
                if part["text"].strip() in {"M", "F"}
                and part["confidence"] >= 0.8
                and bounds[2] < _bounds(part)[0] < genotype[0]
                and _in_row(part, row_y, height)
            ]
            if candidates:
                lines.append(max(candidates, key=lambda part: part["confidence"]))
        if any(
            part["text"].strip() in _MARKS
            and _bounds(part)[0] >= genotype[0] - header_height / 4
            and _in_row(part, row_y, height)
            for part in lines
        ):
            continue
        cell = (
            int(genotype[0] + (genotype[2] - genotype[0]) * 0.15),
            max(0, int(row_y - height * 0.65)),
            right,
            min(image.height, int(row_y + height * 0.55)),
        )
        mark = _read_mark(image.crop(cell))
        if mark is not None:
            text, confidence = mark
            lines.append(
                {
                    "text": text,
                    "confidence": confidence,
                    "box": [
                        [cell[0], cell[1]],
                        [cell[2], cell[1]],
                        [cell[2], cell[3]],
                        [cell[0], cell[3]],
                    ],
                }
            )


def read_photo_lines(payload: bytes) -> list[dict[str, Any]]:
    from PIL import Image, ImageOps, UnidentifiedImageError
    from pillow_heif import register_heif_opener

    register_heif_opener()
    try:
        with Image.open(io.BytesIO(payload)) as source:
            if source.format not in {"JPEG", "PNG", "HEIF", "HEIC"}:
                raise ValueError("Choose a JPEG, PNG, or HEIC photo.")
            if source.width * source.height > 40_000_000:
                raise ValueError("Photo must be at most 40 megapixels.")
            image = ImageOps.exif_transpose(source)
            original_width, original_height = image.size
            image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
            image = image.convert("RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ValueError("Choose a JPEG, PNG, or HEIC photo.") from exc
    lines = _lines(image)
    _complete_table(image, lines)
    for line in lines:
        line["box"] = [
            [x * original_width / image.width, y * original_height / image.height]
            for x, y in line["box"]
        ]
    return lines
