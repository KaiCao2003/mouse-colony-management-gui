from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app

BASE_URL = "http://127.0.0.1:8765"
TEST_ROOM_ALIASES = {
    "ROOM-REGULAR": "Regular Cycle room",
    "ROOM-REVERSE": "Reverse Cycle room",
    "ROOM-BREEDING": "Breeding Core",
}
TEST_BREEDING_ROOMS = {"ROOM-BREEDING", "Breeding Core"}


def _client(tmp_path: Path, *, root_path: str = "") -> TestClient:
    database = Database(
        tmp_path / "route-test.db",
        room_aliases=TEST_ROOM_ALIASES,
        breeding_rooms=TEST_BREEDING_ROOMS,
    )
    app = create_app(
        settings=Settings(
            database_path=tmp_path / "route-test.db",
            seed_on_empty=False,
            root_path=root_path,
        ),
        database=database,
        run_seed=False,
    )
    return TestClient(app, base_url=BASE_URL, client=("127.0.0.1", 50_000))


def _csrf(client: TestClient) -> dict[str, str]:
    page = client.get("/")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text)
    assert token is not None
    return {"Origin": BASE_URL, "X-CSRF-Token": token.group(1)}


def _form_tag(html: str, action: str) -> str:
    match = re.search(rf'<form\b[^>]*action="{re.escape(action)}"[^>]*>', html)
    assert match is not None, f"Missing form for {action}"
    return match.group(0)


def _assert_form_not_inside_details(html: str, action: str) -> None:
    form_tag = _form_tag(html, action)
    form_index = html.index(form_tag)
    last_details_open = html.rfind("<details", 0, form_index)
    last_details_close = html.rfind("</details>", 0, form_index)
    assert last_details_open <= last_details_close, f"Form for {action} is hidden in details"


def _cage_row(html: str, cage_id: int) -> str:
    for row in re.findall(r"<tr\b[^>]*>.*?</tr>", html, flags=re.DOTALL):
        if f'href="/cages/{cage_id}"' in row:
            return row
    raise AssertionError(f"Missing table row for cage {cage_id}")


def test_create_cage_and_render_detail(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/cages/new",
            headers=_csrf(client),
            data={
                "cage_card_id": "CC12345678",
                "count": "3",
                "sex": "F",
                "dob": "2026-06-01",
                "genotype": "WT",
                "room": "ROOM-REGULAR",
                "protocol": "PROTO-TEST",
                "note": "test cage",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        detail = client.get(response.headers["location"])

    assert detail.status_code == 200
    assert "CC12345678" in detail.text
    assert "3 active mice" in detail.text
    assert re.search(r"[A-Z]-\d{4}", detail.text)


def test_posts_require_same_origin_and_csrf(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/cages/new",
            data={"count": "0", "sex": "U"},
            headers={"Origin": BASE_URL},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "invalid_csrf"


def test_root_and_health_are_available_without_login(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        root = client.get("/")
        health = client.get("/healthz")
    assert root.status_code == 200
    assert "Local data · No login required" not in root.text
    assert health.json()["status"] == "ok"


def test_reverse_proxy_root_path_prefixes_links_forms_and_redirects(tmp_path: Path) -> None:
    with _client(tmp_path, root_path="/colony/") as client:
        root = client.get("/")
        token = re.search(r'<meta name="csrf-token" content="([^"]+)">', root.text)
        assert token is not None
        created = client.post(
            "/cages/new",
            headers={"Origin": BASE_URL, "X-CSRF-Token": token.group(1)},
            data={"cage_card_id": "PREFIX-CAGE", "count": "1", "sex": "F"},
            follow_redirects=False,
        )

    assert root.status_code == 200
    assert 'href="/colony/#cages"' in root.text
    assert 'href="/colony/#new-cage"' in root.text
    assert 'action="/colony/cages/new"' in root.text
    assert 'href="/colony/?view=stock#cages"' in root.text
    assert "/colony/static/styles.css" in root.text
    assert created.status_code == 303
    assert created.headers["location"].startswith("/colony/cages/")


def test_stock_view_only_renders_stock_cages(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        database.create_cage(cage_card_id="STOCK-CAGE", animal_count=2)
        database.create_cage(cage_card_id="SINGLE-CAGE", animal_count=1)
        database.create_cage(
            cage_card_id="BREEDING-CAGE",
            animal_count=2,
            is_breeding_pair=True,
        )

        response = client.get("/?view=stock")

    assert response.status_code == 200
    assert "STOCK-CAGE" in response.text
    assert "SINGLE-CAGE" not in response.text
    assert "BREEDING-CAGE" not in response.text


def test_create_and_update_forward_breeding_pair_flag(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        create_response = client.post(
            "/cages/new",
            headers=_csrf(client),
            data={
                "cage_card_id": "BREED-NEW",
                "count": "2",
                "room": "ROOM-REGULAR",
                "protocol": "KEEP-ME",
                "is_breeding_pair": "on",
            },
            follow_redirects=False,
        )
        assert create_response.status_code == 303
        cage_id = int(create_response.headers["location"].split("?", 1)[0].rsplit("/", 1)[1])
        database = client.app.state.database
        created = database.get_cage(cage_id)
        assert created is not None
        assert created["is_breeding_pair"] is True
        assert created["room"] == "ROOM-REGULAR"
        assert created["protocol"] is None

        normal_id = database.create_cage(
            cage_card_id="BREED-UPDATE",
            room="ROOM-REGULAR",
            protocol="KEEP-ME",
        )
        update_response = client.post(
            f"/cages/{normal_id}/update",
            headers=_csrf(client),
            data={
                "cage_card_id": "BREED-REVISED",
                "room": "ROOM-REGULAR",
                "protocol": "MUST-NOT-REPLACE",
                "note": "breeding pair",
                "on_census_date": "2026-03-04",
                "off_census_date": "2026-05-06",
                "is_breeding_pair": "on",
            },
            follow_redirects=False,
        )
        updated = database.get_cage(normal_id)

    assert update_response.status_code == 303
    assert updated is not None
    assert updated["cage_card_id"] == "BREED-REVISED"
    assert updated["is_breeding_pair"] is True
    assert updated["room"] == "ROOM-REGULAR"
    assert updated["on_census_date"] == "2026-03-04"
    assert updated["off_census_date"] == "2026-05-06"
    assert updated["protocol"] == "KEEP-ME"

    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(
            cage_card_id="DETAIL-FORM",
            on_census_date="2026-01-02",
            off_census_date="2026-02-03",
        )
        detail_response = client.get(f"/cages/{cage_id}")

    assert detail_response.status_code == 200
    assert 'name="cage_card_id" value="DETAIL-FORM"' in detail_response.text
    assert 'name="on_census_date" value="2026-01-02"' in detail_response.text
    assert 'name="off_census_date" value="2026-02-03"' in detail_response.text


def test_split_and_wean_ignore_forged_protocol_fields(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        source_id = database.create_cage(
            cage_card_id="PRIVATE-SOURCE",
            animal_count=2,
            protocol="SOURCE-PRIVATE",
            is_breeding_pair=True,
        )
        animals = database.list_animals(source_id)
        headers = _csrf(client)

        split_response = client.post(
            f"/cages/{source_id}/split",
            headers=headers,
            data={
                "animal_ids": str(animals[0]["id"]),
                "destination_cage_card_id": "PRIVATE-SPLIT",
                "destination_protocol": "FORGED-SPLIT",
            },
            follow_redirects=False,
        )
        split_id = int(split_response.headers["location"].split("?", 1)[0].rsplit("/", 1)[1])

        wean_response = client.post(
            f"/cages/{source_id}/wean",
            headers=headers,
            data={
                "count": "2",
                "sex": "F",
                "dob": "2026-07-01",
                "genotype": "WT",
                "destination_cage_card_id": "PRIVATE-WEAN",
                "destination_protocol": "FORGED-WEAN",
            },
            follow_redirects=False,
        )
        wean_id = int(wean_response.headers["location"].split("?", 1)[0].rsplit("/", 1)[1])
        split = database.get_cage(split_id)
        wean = database.get_cage(wean_id)

    assert split_response.status_code == 303
    assert wean_response.status_code == 303
    assert split is not None and split["protocol"] == "SOURCE-PRIVATE"
    assert wean is not None and wean["protocol"] == "SOURCE-PRIVATE"


def test_root_hash_navigation_targets_and_filter_links(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert 'id="cages"' in html
    assert 'id="new-cage"' in html
    assert 'href="/#cages"' in html
    assert 'href="/#new-cage"' in html
    assert 'class="filter-form" method="get" action="/#cages"' in html
    assert 'class="button button--quiet" href="/#cages"' in html
    assert html.count('href="/?status=active&amp;view=all#cages"') >= 2
    assert 'href="/?status=active&amp;view=stock#cages"' in html


def test_cage_rows_expose_full_row_navigation_target(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        cage_id = client.app.state.database.create_cage(cage_card_id="ROW-LINK")
        response = client.get("/")

    assert response.status_code == 200
    row = _cage_row(response.text, cage_id)
    cage_href = f"/cages/{cage_id}"
    assert f'data-cage-row-href="{cage_href}"' in row
    assert f'class="record-link" href="{cage_href}"' in row


def test_stock_and_using_views_render_separately_with_sex_breakdown(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        stock_id = database.create_cage(
            cage_card_id="VIEW-STOCK",
            animal_count=2,
            sex="M",
            genotype="StockHet",
        )
        database.add_animals(stock_id, count=1, sex="F", genotype="StockHet")
        database.add_animals(stock_id, count=1, sex="U", genotype="StockHet")
        using_id = database.create_cage(
            cage_card_id="VIEW-USING",
            animal_count=1,
            sex="F",
            genotype="UseWT",
        )
        breeding_id = database.create_cage(
            cage_card_id="VIEW-BREEDING",
            animal_count=2,
            sex="M",
            room="ROOM-BREEDING",
        )

        stock_response = client.get("/?view=stock")
        using_response = client.get("/?view=using")
        legacy_response = client.get("/?view=single")

    assert (
        stock_response.status_code
        == using_response.status_code
        == legacy_response.status_code
        == 200
    )
    stock_html = stock_response.text
    using_html = using_response.text
    legacy_html = legacy_response.text
    stock_row = _cage_row(stock_html, stock_id)
    using_row = _cage_row(using_html, using_id)

    assert f'href="/cages/{stock_id}"' in stock_html
    assert f'href="/cages/{using_id}"' not in stock_html
    assert f'href="/cages/{breeding_id}"' not in stock_html
    assert 'aria-label="2 male stock mice"' in stock_html
    assert 'aria-label="1 female stock mouse"' in stock_html
    assert "Unknown 1" in stock_html
    stock_heading = (
        '<span class="metric-card__label">Stock mice</span><span class="metric-card__hint">'
    )
    assert stock_heading in stock_html
    assert "view-switcher__label" not in stock_html
    assert '<th scope="col">Sex</th><th scope="col">Genotype</th>' in stock_html
    assert 'aria-label="2 male mice"' in stock_row
    assert 'aria-label="1 female mouse"' in stock_row
    assert "Unknown 1" in stock_row
    assert '<td data-label="Genotype">StockHet</td>' in stock_row
    assert 'data-label="Details"' not in stock_row

    assert f'href="/cages/{using_id}"' in using_html
    assert f'href="/cages/{stock_id}"' not in using_html
    assert f'href="/cages/{breeding_id}"' not in using_html
    assert 'aria-label="1 female mouse"' in using_row
    assert "♀ 1" in using_row
    assert "♂" not in using_row
    assert '<td data-label="Genotype">UseWT</td>' in using_row
    assert '<option value="F">♀ Female</option>' in using_html
    assert '<option value="M">♂ Male</option>' in using_html

    active_using_link = (
        'class="view-switcher__link is-active" href="/?view=using#cages" '
        'aria-current="page">In-use mice</a>'
    )
    assert active_using_link in using_html
    assert active_using_link in legacy_html
    assert '<input type="hidden" name="view" value="using">' in legacy_html


def test_view_switcher_preserves_current_filters(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get(
            "/",
            params={
                "view": "stock",
                "search": "Alpha & Beta",
                "status": "active",
                "tag": "Group & One",
            },
        )

    assert response.status_code == 200
    for view_name, label in (
        ("all", "All cages"),
        ("stock", "Stock mice"),
        ("using", "In-use mice"),
        ("breeding", "Breeding pairs"),
    ):
        match = re.search(rf'href="([^"]+)"[^>]*>{re.escape(label)}</a>', response.text)
        assert match is not None
        query = parse_qs(urlsplit(unescape(match.group(1))).query)
        assert query == {
            "view": [view_name],
            "search": ["Alpha & Beta"],
            "status": ["active"],
            "tag": ["Group & One"],
        }


def test_update_animal_persists_every_editable_detail(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(cage_card_id="EDIT-MOUSE", animal_count=1)
        animal_id = int(database.list_animals(cage_id)[0]["id"])

        response = client.post(
            f"/animals/{animal_id}/update",
            headers=_csrf(client),
            data={
                "legacy_id": "  Prior-007  ",
                "sex": "F",
                "dob": "2026-02-03",
                "genotype": "  Cre+ / WT  ",
                "note": "  monitor after weaning  ",
            },
            follow_redirects=False,
        )
        updated = database.get_animal(animal_id)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/cages/{cage_id}?")
    assert updated is not None
    assert updated["legacy_id"] == "Prior-007"
    assert updated["sex"] == "F"
    assert updated["dob"] == "2026-02-03"
    assert updated["genotype"] == "Cre+ / WT"
    assert updated["note"] == "monitor after weaning"


def test_add_remove_and_restore_mouse_updates_active_count(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(cage_card_id="ADJUST-MICE", animal_count=1)
        original_ids = {int(animal["id"]) for animal in database.list_animals(cage_id)}
        headers = _csrf(client)

        added_response = client.post(
            f"/cages/{cage_id}/add-mice",
            headers=headers,
            data={
                "count": "2",
                "sex": "F",
                "dob": "2026-05-06",
                "genotype": "WT",
                "note": "route test",
            },
            follow_redirects=False,
        )
        after_add = database.get_cage(cage_id)
        added_animals = [
            animal
            for animal in database.list_animals(cage_id)
            if int(animal["id"]) not in original_ids
        ]
        assert len(added_animals) == 2
        removed_id = int(added_animals[0]["id"])

        removed_response = client.post(
            f"/animals/{removed_id}/toggle",
            headers=headers,
            follow_redirects=False,
        )
        after_remove = database.get_cage(cage_id)
        restored_response = client.post(
            f"/animals/{removed_id}/toggle",
            headers=headers,
            follow_redirects=False,
        )
        after_restore = database.get_cage(cage_id)

    assert added_response.status_code == 303
    assert removed_response.status_code == 303
    assert restored_response.status_code == 303
    assert after_add is not None and after_add["active_count"] == 3
    assert after_remove is not None and after_remove["active_count"] == 2
    assert after_restore is not None and after_restore["active_count"] == 3


def test_update_surgery_route_persists_fields_and_keeps_record_count(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(cage_card_id="EDIT-SURGERY", animal_count=1)
        animal_id = int(database.list_animals(cage_id)[0]["id"])
        surgery_ids: list[int] = []
        for index in range(4):
            surgery_id = database.add_surgery(
                animal_id,
                surgery_date=f"2026-03-{index + 1:02d}",
                surgery_time="08:00",
                operator="Original Operator",
                surgery_type="Headplate",
            )
            assert surgery_id is not None
            surgery_ids.append(surgery_id)

        response = client.post(
            f"/surgeries/{surgery_ids[0]}/update",
            headers=_csrf(client),
            data={
                "surgery_date": "2026-04-05",
                "surgery_time": "13:45",
                "operator": "  Revised Operator  ",
                "surgery_type": "Probe implant",
            },
            follow_redirects=False,
        )
        updated = database.get_surgery(surgery_ids[0])
        animal = database.get_animal(animal_id)
        missing = client.post(
            "/surgeries/999999/update",
            headers=_csrf(client),
            data={
                "surgery_date": "2026-04-06",
                "operator": "Nobody",
                "surgery_type": "Headplate",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/cages/{cage_id}?")
    assert updated is not None
    assert updated["animal_id"] == animal_id
    assert updated["cage_id"] == cage_id
    assert updated["surgery_date"] == "2026-04-05"
    assert updated["surgery_time"] == "13:45"
    assert updated["operator"] == "Revised Operator"
    assert updated["surgery_type"] == "Probe implant"
    assert animal is not None and len(animal["surgeries"]) == 4
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Surgery record not found."


def test_cage_hash_targets_and_form_return_locations(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(
            cage_card_id="HASH-CAGE",
            animal_count=1,
            is_breeding_pair=True,
        )
        database.add_tag(cage_id, "hash-test")
        cage = database.get_cage(cage_id)
        assert cage is not None
        animal = database.list_animals(cage_id)[0]
        surgery_id = database.add_surgery(
            animal["id"],
            surgery_date="2026-06-07",
            surgery_time="10:30",
            operator="Hash Operator",
            surgery_type="Headplate",
        )
        assert surgery_id is not None
        tag_id = cage["tags"][0]["id"]
        response = client.get(f"/cages/{cage_id}")

    assert response.status_code == 200
    html = response.text
    mouse_hash = f"#mouse-{animal['id']}"
    for target_id in (
        "cage-details",
        "cage-actions",
        "mice",
        f"mouse-{animal['id']}",
    ):
        assert f'id="{target_id}"' in html

    for action in (
        f"/cages/{cage_id}/toggle",
        f"/cages/{cage_id}/update",
        f"/cages/{cage_id}/tags",
        f"/cages/{cage_id}/tags/{tag_id}/remove",
    ):
        assert 'data-return-hash="#cage-details"' in _form_tag(html, action)

    for action in (
        f"/cages/{cage_id}/add-mice",
        f"/cages/{cage_id}/split",
        f"/cages/{cage_id}/wean",
    ):
        assert 'data-return-hash="#cage-actions"' in _form_tag(html, action)

    for action in (
        f"/animals/{animal['id']}/surgery",
        f"/animals/{animal['id']}/update",
        f"/animals/{animal['id']}/toggle",
        f"/surgeries/{surgery_id}/update",
    ):
        assert f'data-return-hash="{mouse_hash}"' in _form_tag(html, action)

    for action in (
        f"/cages/{cage_id}/update",
        f"/cages/{cage_id}/add-mice",
        f"/animals/{animal['id']}/update",
        f"/surgeries/{surgery_id}/update",
    ):
        _assert_form_not_inside_details(html, action)

    assert 'name="legacy_id"' in html
    assert "Remove mouse" in html
    assert "Configured breeding rooms" not in html
    assert "Local data · No login required" not in html
