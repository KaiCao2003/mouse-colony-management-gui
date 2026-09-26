from __future__ import annotations

import copy
import hashlib
import io
import json
import re
from pathlib import Path

import pytest
from test_routes import _assert_form_not_inside_details, _client, _colony_snapshot, _csrf

from app import photo_import


@pytest.fixture
def photo_bytes() -> bytes:
    image = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    image.new("RGB", (24, 16), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _sample(index: int) -> list[dict]:
    # Sanitized OCR text retains the photo boxes and row geometry.
    return json.loads((Path(__file__).parent / "fixtures/card_ocr.json").read_text())[index][
        "lines"
    ]


@pytest.mark.parametrize(
    "index,cage,ids",
    [
        (0, "CC00001234", ["10001", "10002", "10003"]),
        (1, "CC00001235", ["10004", "10005", "10006"]),
    ],
)
def test_ocr_rows_keep_ids_dates_and_sexes_together(
    index: int, cage: str, ids: list[str]
) -> None:
    result = photo_import.parse_card_lines(_sample(index))
    assert result["cage_card_id"] == cage
    assert result["line"] == "Example-Cre line 8"
    assert [row["mouse_id"] for row in result["rows"]] == ids
    assert all(row["sex"] == "M" and row["dob"] == "2026-01-15" for row in result["rows"])
    if index == 1:
        assert all(row["genotype"] is None for row in result["rows"])
    else:
        assert [row["genotype"] for row in result["rows"]] == ["+", "-", None]


def test_missing_values_and_multiple_cards_are_not_guessed() -> None:
    lines = _sample(1)
    lines = [part for part in lines if part["text"] not in {"M", "01/15/26"}]
    lines.append({**lines[0], "text": "Cage: CC00000001"})
    result = photo_import.parse_card_lines(lines)
    assert result["cage_card_id"] is None
    assert len(result["rows"]) == 3
    assert all(row["sex"] is None and row["dob"] is None for row in result["rows"])
    assert photo_import.parse_card_lines([])["rows"] == []


def test_photo_preview_matches_cage_and_leaves_records_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, photo_bytes: bytes
) -> None:
    recognized = photo_import.parse_card_lines(_sample(0))
    monkeypatch.setattr(photo_import, "recognize_photo", lambda payload: copy.deepcopy(recognized))
    with _client(tmp_path, root_path="/colony") as client:
        database = client.app.state.database
        cage_id = database.create_cage(cage_card_id="CC00001234", animal_count=3)
        before = database.list_animals(cage_id)
        response = client.post(
            "/photos/recognize",
            files={"photo_file": ("card.png", photo_bytes)},
            headers=_csrf(client),
        )
        assert response.status_code == 200
        assert response.json()["cage_id"] == cage_id
        assert database.list_animals(cage_id) == before
        page = client.get(f"/cages/{cage_id}")
        assert 'action="/colony/photos/recognize"' in page.text
        assert "photo-import.js" in page.text
        assert "data-save-all-mice" in page.text
        home = client.get("/")
        _assert_form_not_inside_details(home.text, "/colony/photos/recognize")
        assert 'type="file" name="photo_file"' in home.text
        assert (
            client.post("/photos/recognize", files={"photo_file": ("x", b"x")}).status_code == 403
        )


@pytest.mark.parametrize("cage_status", ["active", "inactive", "missing"])
def test_photo_entry_resolves_existing_cages_without_changing_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cage_status: str, photo_bytes: bytes
) -> None:
    recognized = photo_import.parse_card_lines(_sample(0))
    monkeypatch.setattr(photo_import, "recognize_photo", lambda payload: copy.deepcopy(recognized))
    with _client(tmp_path, root_path="/colony") as client:
        database = client.app.state.database
        cage_id = None
        if cage_status != "missing":
            cage_id = database.create_cage(cage_card_id="CC00001234", animal_count=3)
            if cage_status == "inactive":
                database.toggle_cage(cage_id)
        before = _colony_snapshot(database)
        response = client.post(
            "/photos/recognize",
            files={"photo_file": ("card.png", photo_bytes)},
            headers=_csrf(client),
        )
        assert response.status_code == 200
        assert response.json()["cage_id"] == cage_id
        assert response.json()["rows"] == recognized["rows"]
        assert _colony_snapshot(database) == before
        if cage_id is not None:
            assert client.get(f"/cages/{cage_id}").status_code == 200


def test_photo_errors_release_worker_and_reject_large_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, photo_bytes: bytes
) -> None:
    def fail(payload: bytes) -> dict:
        raise ValueError("Choose a JPEG, PNG, or HEIC photo.")

    monkeypatch.setattr(photo_import, "recognize_photo", fail)
    with _client(tmp_path) as client:
        headers = _csrf(client)
        for _ in range(2):
            response = client.post(
                "/photos/recognize",
                files={"photo_file": ("card.png", photo_bytes)},
                headers=headers,
            )
            assert response.status_code == 422
        response = client.post(
            "/photos/recognize",
            content=b"x",
            headers={**headers, "content-length": str(photo_import.MAX_PHOTO_BYTES + 100_000)},
        )
        assert response.status_code == 413
        photo_import._ocr_lock.acquire()
        try:
            response = client.post(
                "/photos/recognize",
                files={"photo_file": ("card.png", photo_bytes)},
                headers=headers,
            )
            assert response.status_code == 429
        finally:
            photo_import._ocr_lock.release()


def test_cage_photos_preserve_originals_deduplicate_and_survive_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, photo_bytes: bytes
) -> None:
    image = pytest.importorskip("PIL.Image")
    recognized = photo_import.parse_card_lines(_sample(0))
    monkeypatch.setattr(photo_import, "recognize_photo", lambda payload: copy.deepcopy(recognized))
    with _client(tmp_path, root_path="/colony") as client:
        cage_id = client.app.state.database.create_cage(cage_card_id="CC00001234", animal_count=3)
        page = client.get(f"/cages/{cage_id}")
        gallery_tag = re.search(r"<details\b[^>]*data-photo-gallery[^>]*>", page.text)
        assert gallery_tag is not None and " hidden" in gallery_tag.group()
        assert " open" not in gallery_tag.group()
        assert client.get(f"/cages/{cage_id}/photos").json() == {"photos": []}
        response = client.post(
            "/photos/recognize",
            files={"photo_file": ("card.png", photo_bytes)},
            headers=_csrf(client),
        )
        assert response.status_code == 200
        photo = response.json()["photo"]
        assert photo["id"] == hashlib.sha256(photo_bytes).hexdigest()
        assert photo["cage_id"] == cage_id
        assert photo["filename"] == "card.png" and photo["created_at"]
        assert photo["recognition"] == recognized
        assert photo["preview_url"].startswith(f"/colony/cages/{cage_id}/photos/")
        original = client.get(photo["original_url"])
        assert original.status_code == 200 and original.content == photo_bytes
        preview = client.get(photo["preview_url"])
        assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
        with image.open(io.BytesIO(preview.content)) as decoded:
            assert decoded.format == "JPEG" and decoded.size == (24, 16)
        repeat = client.post(
            "/photos/recognize",
            files={"photo_file": ("card-copy.png", photo_bytes)},
            headers=_csrf(client),
        )
        assert repeat.status_code == 200 and repeat.json()["photo"]["id"] == photo["id"]
        assert repeat.json()["photo"]["recognition"] == recognized
        assert len(client.get(f"/cages/{cage_id}/photos").json()["photos"]) == 1
        assert len(list((tmp_path / "photos").rglob("original.*"))) == 1
    with _client(tmp_path, root_path="/colony") as restarted:
        photos = restarted.get(f"/cages/{cage_id}/photos").json()["photos"]
        assert len(photos) == 1 and photos[0]["id"] == photo["id"]
        assert photos[0]["recognition"] == recognized
        assert restarted.get(photos[0]["original_url"]).content == photo_bytes
        database = restarted.app.state.database
        mouse = database.list_animals(cage_id)[0]
        database.update_animal(mouse["id"], dob="2026-02-20")
        assert database.get_animal(mouse["id"])["dob"] == "2026-02-20"
        assert restarted.get(f"/cages/{cage_id}/photos").json()["photos"][0][
            "recognition"
        ] == recognized
        page = restarted.get(f"/cages/{cage_id}")
        gallery_tag = re.search(r"<details\b[^>]*data-photo-gallery[^>]*>", page.text)
        assert gallery_tag is not None
        assert " open" not in gallery_tag.group() and " hidden" not in gallery_tag.group()
        assert photos[0]["preview_url"] in page.text
        assert "Download original" in page.text
        gallery = re.search(
            r"<details\b[^>]*data-photo-gallery[^>]*>.*?</details>", page.text, flags=re.DOTALL
        )
        assert gallery is not None
        assert ">DOB<" in gallery.group()
        for row in recognized["rows"]:
            assert row["mouse_id"] in gallery.group() and row["dob"] in gallery.group()
        assert recognized["line"] in gallery.group()


def test_repeated_recognition_updates_only_snapshot_and_failure_preserves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, photo_bytes: bytes
) -> None:
    recognized = photo_import.parse_card_lines(_sample(0))
    monkeypatch.setattr(photo_import, "recognize_photo", lambda payload: copy.deepcopy(recognized))
    with _client(tmp_path) as client:
        cage_id = client.app.state.database.create_cage(cage_card_id="CC00001234")
        headers = _csrf(client)
        first = client.post(
            "/photos/recognize", files={"photo_file": ("first.png", photo_bytes)}, headers=headers
        )
        assert first.status_code == 200
        original_photo = first.json()["photo"]
        assert original_photo["recognition"] == recognized
        recognized["rows"][0].update({"dob": "2026-02-21", "sex": "F", "genotype": "WT"})
        repeated = client.post(
            "/photos/recognize", files={"photo_file": ("second.png", photo_bytes)}, headers=headers
        )
        assert repeated.status_code == 200
        updated_photo = repeated.json()["photo"]
        for key in ("id", "filename", "created_at", "cage_id"):
            assert updated_photo[key] == original_photo[key]
        assert updated_photo["recognition"] == recognized

        def fail(payload: bytes) -> dict:
            raise RuntimeError("Recognition models are unavailable.")

        monkeypatch.setattr(photo_import, "recognize_photo", fail)
        failed = client.post(
            "/photos/recognize",
            files={"photo_file": ("third.png", photo_bytes)},
            data={"cage_id": str(cage_id)},
            headers=headers,
        )
        assert failed.status_code == 503
        stored = client.get(f"/cages/{cage_id}/photos").json()["photos"]
        assert stored == [updated_photo]
        assert client.get(stored[0]["original_url"]).content == photo_bytes


@pytest.mark.parametrize("recognized_card", [None, "CC00001234"])
def test_photo_uses_supplied_cage_only_when_no_other_cage_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, photo_bytes: bytes,
    recognized_card: str | None,
) -> None:
    recognized = {"cage_card_id": recognized_card, "line": None, "rows": []}
    monkeypatch.setattr(photo_import, "recognize_photo", lambda payload: copy.deepcopy(recognized))
    with _client(tmp_path) as client:
        database = client.app.state.database
        source = database.create_cage(cage_card_id="CC00001235")
        recognized_cage = database.create_cage(cage_card_id="CC00001234")
        response = client.post(
            "/photos/recognize",
            files={"photo_file": ("card.png", photo_bytes)},
            data={"cage_id": str(source)},
            headers=_csrf(client),
        )
        assert response.status_code == 200
        destination = recognized_cage if recognized_card else source
        other = source if recognized_card else recognized_cage
        assert response.json()["photo"]["cage_id"] == destination
        assert len(client.get(f"/cages/{destination}/photos").json()["photos"]) == 1
        assert client.get(f"/cages/{other}/photos").json() == {"photos": []}


@pytest.mark.parametrize("error,status", [(ValueError, 422), (RuntimeError, 503)])
@pytest.mark.parametrize("supplied_cage", [False, True])
def test_valid_photos_are_kept_when_recognition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, photo_bytes: bytes,
    error: type[Exception], status: int, supplied_cage: bool,
) -> None:
    def fail(payload: bytes) -> dict:
        raise error("Could not read this card.")

    monkeypatch.setattr(photo_import, "recognize_photo", fail)
    with _client(tmp_path) as client:
        cage_id = client.app.state.database.create_cage(cage_card_id="CC00001234")
        response = client.post(
            "/photos/recognize",
            files={"photo_file": ("card.png", photo_bytes)},
            data={"cage_id": str(cage_id)} if supplied_cage else {},
            headers=_csrf(client),
        )
        assert response.status_code == status
        assert response.json()["detail"].startswith("Photo saved.")
        folder = str(cage_id) if supplied_cage else "unassigned"
        original = tmp_path / "photos" / folder / hashlib.sha256(photo_bytes).hexdigest()
        assert (original / "original.png").read_bytes() == photo_bytes
        photos = client.get(f"/cages/{cage_id}/photos").json()["photos"]
        assert len(photos) == int(supplied_cage)


def test_photo_media_requires_login_and_rejects_wrong_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, photo_bytes: bytes
) -> None:
    recognized = photo_import.parse_card_lines(_sample(0))
    monkeypatch.setattr(photo_import, "recognize_photo", lambda payload: copy.deepcopy(recognized))
    with _client(tmp_path, root_path="/colony") as client:
        cage_id = client.app.state.database.create_cage(cage_card_id="CC00001234")
        other_id = client.app.state.database.create_cage(cage_card_id="CC00001235")
        response = client.post(
            "/photos/recognize",
            files={"photo_file": ("card.png", photo_bytes)},
            headers=_csrf(client),
        )
        assert response.status_code == 200
        photo = response.json()["photo"]
        for kind in ("original", "preview"):
            assert client.get(f"/cages/{other_id}/photos/{photo['id']}/{kind}").status_code == 404
            for invalid in ("invalid-id", "a" * 64, "%2E%2E%2Fmetadata.json"):
                assert client.get(f"/cages/{cage_id}/photos/{invalid}/{kind}").status_code == 404
        assert client.get("/cages/99999/photos").status_code == 404
        client.cookies.clear()
        for url in (photo["original_url"], photo["preview_url"], f"/cages/{cage_id}/photos"):
            blocked = client.get(url, follow_redirects=False)
            assert blocked.status_code in (302, 303, 307)
            assert "/colony/login" in blocked.headers["location"]


def test_invalid_and_oversize_uploads_do_not_create_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, photo_bytes: bytes
) -> None:
    def must_not_recognize(payload: bytes) -> dict:
        pytest.fail("Invalid and oversized files must be rejected before OCR.")

    monkeypatch.setattr(photo_import, "recognize_photo", must_not_recognize)
    with _client(tmp_path) as client:
        headers = _csrf(client)
        invalid = client.post(
            "/photos/recognize",
            files={"photo_file": ("card.png", b"not an image")},
            headers=headers,
        )
        assert invalid.status_code == 422
        monkeypatch.setattr(photo_import, "MAX_PHOTO_BYTES", len(photo_bytes) - 1)
        oversized = client.post(
            "/photos/recognize", files={"photo_file": ("card.png", photo_bytes)}, headers=headers
        )
        assert oversized.status_code == 413
        assert not list((tmp_path / "photos").rglob("original.*"))


@pytest.mark.parametrize("operation", ["save", "assign", "save_recognition"])
def test_storage_failures_are_reported_without_false_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, photo_bytes: bytes, operation: str
) -> None:
    from app.photo_storage import PhotoStore

    def fail(*args: object, **kwargs: object) -> dict:
        raise OSError("Disk is unavailable.")

    recognized = photo_import.parse_card_lines(_sample(0))
    monkeypatch.setattr(photo_import, "recognize_photo", lambda payload: copy.deepcopy(recognized))
    monkeypatch.setattr(PhotoStore, operation, fail)
    with _client(tmp_path) as client:
        client.app.state.database.create_cage(cage_card_id="CC00001234")
        response = client.post(
            "/photos/recognize",
            files={"photo_file": ("card.png", photo_bytes)},
            headers=_csrf(client),
        )
        assert response.status_code == 503
        assert "detail" in response.json() and "photo" not in response.json()
