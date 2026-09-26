from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_routes import _assert_form_not_inside_details, _client, _colony_snapshot, _csrf

from app import photo_import


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recognized = photo_import.parse_card_lines(_sample(0))
    monkeypatch.setattr(photo_import, "recognize_photo", lambda payload: copy.deepcopy(recognized))
    with _client(tmp_path, root_path="/colony") as client:
        database = client.app.state.database
        cage_id = database.create_cage(cage_card_id="CC00001234", animal_count=3)
        before = database.list_animals(cage_id)
        response = client.post(
            "/photos/recognize", files={"photo_file": ("card.heic", b"test")}, headers=_csrf(client)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cage_status: str
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
            "/photos/recognize", files={"photo_file": ("card.heic", b"test")}, headers=_csrf(client)
        )
        assert response.status_code == 200
        assert response.json()["cage_id"] == cage_id
        assert response.json()["rows"] == recognized["rows"]
        assert _colony_snapshot(database) == before
        if cage_id is not None:
            assert client.get(f"/cages/{cage_id}").status_code == 200


def test_photo_errors_release_worker_and_reject_large_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(payload: bytes) -> dict:
        raise ValueError("Choose a JPEG, PNG, or HEIC photo.")

    monkeypatch.setattr(photo_import, "recognize_photo", fail)
    with _client(tmp_path) as client:
        headers = _csrf(client)
        for _ in range(2):
            response = client.post(
                "/photos/recognize", files={"photo_file": ("x", b"bad")}, headers=headers
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
                "/photos/recognize", files={"photo_file": ("x", b"bad")}, headers=headers
            )
            assert response.status_code == 429
        finally:
            photo_import._ocr_lock.release()
