"""Keep original cage photos and browser previews on the shared drive."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import re
import shutil
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_PHOTO_ID = re.compile(r"[a-f0-9]{64}")
_ORIGINALS = {"original.jpg", "original.png", "original.heic"}


def _preview(payload: bytes) -> tuple[str, bytes]:
    from PIL import Image, ImageOps, UnidentifiedImageError
    from pillow_heif import register_heif_opener

    register_heif_opener()
    try:
        with Image.open(io.BytesIO(payload)) as source:
            extension = {"JPEG": "jpg", "PNG": "png", "HEIF": "heic", "HEIC": "heic"}.get(
                source.format or ""
            )
            if extension is None:
                raise ValueError("Choose a JPEG, PNG, or HEIC photo.")
            if source.width * source.height > 40_000_000:
                raise ValueError("Photo must be at most 40 megapixels.")
            image = ImageOps.exif_transpose(source)
            image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
            rgba = image.convert("RGBA")
            preview = Image.new("RGB", image.size, "white")
            preview.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            preview.save(output, format="JPEG", quality=90)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ValueError("Choose a JPEG, PNG, or HEIC photo.") from exc
    return extension, output.getvalue()


class PhotoStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _directory(self, cage_id: int | None, photo_id: str) -> Path:
        if not _PHOTO_ID.fullmatch(photo_id):
            raise FileNotFoundError("Photo not found.")
        if cage_id is not None and (type(cage_id) is not int or cage_id <= 0):
            raise FileNotFoundError("Cage not found.")
        return self.root / (str(cage_id) if cage_id is not None else "unassigned") / photo_id

    @staticmethod
    def _metadata(directory: Path) -> dict[str, Any]:
        return json.loads((directory / "metadata.json").read_text(encoding="utf-8"))

    @staticmethod
    def _original(directory: Path) -> Path:
        originals = [path for path in directory.iterdir() if path.name in _ORIGINALS]
        if len(originals) != 1:
            raise FileNotFoundError("Original photo not found.")
        return originals[0]

    def _publish(
        self, directory: Path, metadata: dict[str, Any], files: dict[str, bytes]
    ) -> dict[str, Any]:
        directory.parent.mkdir(parents=True, exist_ok=True)
        # A complete directory is made visible at once, including on the network drive.
        with TemporaryDirectory(prefix=".upload-", dir=directory.parent) as temporary:
            staging = Path(temporary)
            for name, payload in files.items():
                (staging / name).write_bytes(payload)
            (staging / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
            )
            try:
                staging.rename(directory)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                return self._metadata(directory)
        return metadata

    def save(self, payload: bytes, filename: str, cage_id: int | None = None) -> dict[str, Any]:
        photo_id = hashlib.sha256(payload).hexdigest()
        directory = self._directory(cage_id, photo_id)
        try:
            return self._metadata(directory)
        except FileNotFoundError:
            pass
        extension, preview = _preview(payload)
        metadata = {
            "id": photo_id,
            "filename": filename.replace("\\", "/").rsplit("/", 1)[-1] or "photo",
            "created_at": datetime.now(UTC).isoformat(),
            "cage_id": cage_id,
        }
        return self._publish(
            directory, metadata, {f"original.{extension}": payload, "preview.jpg": preview}
        )

    def assign(self, photo: dict[str, Any], cage_id: int) -> dict[str, Any]:
        target = self._directory(cage_id, photo["id"])
        if photo["cage_id"] == cage_id:
            return self._metadata(target)
        if photo["cage_id"] is not None:
            raise ValueError("Photo is already assigned to a cage.")
        source = self._directory(None, photo["id"])
        try:
            result = self._metadata(target)
        except FileNotFoundError:
            metadata = {**self._metadata(source), "cage_id": cage_id}
            original = self._original(source)
            result = self._publish(
                target,
                metadata,
                {
                    original.name: original.read_bytes(),
                    "preview.jpg": (source / "preview.jpg").read_bytes(),
                },
            )
        with suppress(FileNotFoundError):
            shutil.rmtree(source)
        return result

    def list(self, cage_id: int) -> list[dict[str, Any]]:
        parent = self._directory(cage_id, "0" * 64).parent
        try:
            photos = [
                self._metadata(path)
                for path in parent.iterdir()
                if _PHOTO_ID.fullmatch(path.name) and path.is_dir()
            ]
        except FileNotFoundError:
            if not parent.exists():
                return []
            raise
        return sorted(photos, key=lambda photo: (photo["created_at"], photo["id"]), reverse=True)

    def file(self, cage_id: int, photo_id: str, kind: str) -> tuple[Path, dict[str, Any]]:
        if kind not in {"preview", "original"}:
            raise FileNotFoundError("Photo not found.")
        directory = self._directory(cage_id, photo_id)
        metadata = self._metadata(directory)
        path = directory / "preview.jpg" if kind == "preview" else self._original(directory)
        path.stat()
        return path, metadata
