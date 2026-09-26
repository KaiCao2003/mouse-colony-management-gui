from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest

from app.photo_storage import PhotoStore

Image = pytest.importorskip("PIL.Image")
pytest.importorskip("pillow_heif")


def _image(format: str = "PNG", **kwargs: Any) -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (30, 20), "red")
    image.save(output, format=format, **kwargs)
    return output.getvalue()


@pytest.mark.parametrize("format,extension", [("PNG", "png"), ("JPEG", "jpg")])
def test_original_bytes_survive_assignment_and_store_reload(
    tmp_path: Path, format: str, extension: str
) -> None:
    payload = _image(format)
    store = PhotoStore(tmp_path / "IMGS")
    photo = store.save(payload, "../../card.heic")
    assert photo["id"] == hashlib.sha256(payload).hexdigest()
    assert photo["filename"] == "card.heic"
    assert photo["cage_id"] is None
    unassigned = store.root / "unassigned" / photo["id"]
    assert (unassigned / f"original.{extension}").read_bytes() == payload
    assert store.list(84) == []

    assigned = store.assign(photo, 84)
    reloaded = PhotoStore(store.root)
    assert not unassigned.exists()
    assert reloaded.list(84) == [assigned]
    path, metadata = reloaded.file(84, photo["id"], "original")
    assert path.name == f"original.{extension}"
    assert path.read_bytes() == payload
    assert metadata == assigned
    with Image.open(reloaded.file(84, photo["id"], "preview")[0]) as preview:
        assert preview.format == "JPEG"
        assert preview.size == (30, 20)
    assert reloaded.assign(assigned, 84) == assigned


def test_duplicate_upload_keeps_first_metadata_and_original(tmp_path: Path) -> None:
    store = PhotoStore(tmp_path)
    payload = _image()
    first = store.save(payload, "first.png", 84)
    assert store.save(payload, "second.png", 84) == first
    unassigned = store.save(payload, r"C:\fakepath\third.png")
    assert unassigned["filename"] == "third.png"
    assert store.assign(unassigned, 84) == first
    assert store.list(84) == [first]
    assert not (tmp_path / "unassigned" / first["id"]).exists()
    assert store.file(84, first["id"], "original")[0].read_bytes() == payload


def test_preview_orients_resizes_and_flattens_transparency(tmp_path: Path) -> None:
    store = PhotoStore(tmp_path)
    image = Image.new("RGBA", (3000, 1000), (255, 0, 0, 0))
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, format="PNG", exif=exif)
    payload = output.getvalue()
    photo = store.save(payload, "rotated.png", 84)
    assert store.file(84, photo["id"], "original")[0].read_bytes() == payload
    with Image.open(store.file(84, photo["id"], "preview")[0]) as preview:
        assert preview.size == (800, 2400)
        assert preview.mode == "RGB"
        assert preview.getpixel((0, 0)) == (255, 255, 255)


def test_heic_original_is_preserved_with_jpeg_preview(tmp_path: Path) -> None:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    payload = _image("HEIF")
    store = PhotoStore(tmp_path)
    photo = store.save(payload, "card.HEIC", 84)
    original, _ = store.file(84, photo["id"], "original")
    assert original.name == "original.heic"
    assert original.read_bytes() == payload
    with Image.open(store.file(84, photo["id"], "preview")[0]) as preview:
        assert preview.format == "JPEG"


@pytest.mark.parametrize("payload", [b"not an image", _image("GIF"), _image()[:35]])
def test_invalid_photos_leave_no_published_files(tmp_path: Path, payload: bytes) -> None:
    store = PhotoStore(tmp_path / "IMGS")
    with pytest.raises(ValueError, match="Choose a JPEG, PNG, or HEIC photo"):
        store.save(payload, "fake.jpg", 84)
    assert not store.root.exists()


def test_oversized_dimensions_are_rejected_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = Image.new("RGB", (1, 1))
    image.format = "PNG"
    image._size = (8000, 6000)
    monkeypatch.setattr(Image, "open", lambda *args, **kwargs: image)
    with pytest.raises(ValueError, match="40 megapixels"):
        PhotoStore(tmp_path).save(b"large", "large.png")


@pytest.mark.parametrize(
    "cage_id,photo_id,kind",
    [
        (84, "../../etc/passwd", "original"),
        (84, "a" * 64, "../../etc/passwd"),
        (84, "A" * 64, "preview"),
        (0, "a" * 64, "preview"),
        (-1, "a" * 64, "preview"),
        ("../unassigned", "a" * 64, "preview"),
    ],
)
def test_file_rejects_invalid_paths(tmp_path: Path, cage_id: Any, photo_id: str, kind: str) -> None:
    with pytest.raises(FileNotFoundError):
        PhotoStore(tmp_path).file(cage_id, photo_id, kind)


def test_missing_photos_and_cages(tmp_path: Path) -> None:
    store = PhotoStore(tmp_path / "missing")
    assert store.list(84) == []
    with pytest.raises(FileNotFoundError):
        store.file(84, "a" * 64, "preview")


def test_failed_publish_leaves_no_visible_photo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = Path.write_bytes

    def fail_preview(path: Path, payload: bytes) -> int:
        if path.name == "preview.jpg":
            raise OSError("Shared drive is full")
        return original_write(path, payload)

    monkeypatch.setattr(Path, "write_bytes", fail_preview)
    store = PhotoStore(tmp_path)
    with pytest.raises(OSError, match="Shared drive is full"):
        store.save(_image(), "card.png", 84)
    assert store.list(84) == []
    assert list((tmp_path / "84").iterdir()) == []


def test_failed_assignment_preserves_unassigned_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PhotoStore(tmp_path)
    payload = _image()
    photo = store.save(payload, "card.png")

    def fail_rename(*args: Any, **kwargs: Any) -> None:
        raise OSError("Shared drive is unavailable")

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(OSError, match="Shared drive is unavailable"):
        store.assign(photo, 84)
    assert (tmp_path / "unassigned" / photo["id"] / "original.png").read_bytes() == payload
    assert store.list(84) == []


def test_listing_does_not_hide_storage_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_list(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("Shared drive denied access")

    monkeypatch.setattr(Path, "iterdir", fail_list)
    with pytest.raises(PermissionError, match="denied access"):
        PhotoStore(tmp_path).list(84)
