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
    match = re.search(
        rf'<form\b[^>]*action="{re.escape(action)}(?:\?[^\"]*)?"[^>]*>',
        html,
    )
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
        if re.search(rf'href="/cages/{cage_id}(?:\?[^\"]*)?"', row):
            return row
    raise AssertionError(f"Missing table row for cage {cage_id}")


def _sort_header(html: str, field: str) -> tuple[str, str]:
    match = re.search(
        rf'<th\b[^>]*data-sort-field="{re.escape(field)}"[^>]*>.*?</th>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None, f"Missing sortable header for {field}"
    header = match.group(0)
    link = re.search(r'<a\b[^>]*href="([^"]+)"', header)
    assert link is not None, f"Missing sort link for {field}"
    return header, unescape(link.group(1))


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
        detail = client.get(created.headers["location"])

    assert root.status_code == 200
    assert 'href="/colony/#cages"' in root.text
    assert 'href="/colony/#new-cage"' in root.text
    assert 'action="/colony/cages/new"' in root.text
    assert 'href="/colony/?view=stock#cages"' in root.text
    assert "/colony/static/styles.css" in root.text
    assert created.status_code == 303
    assert created.headers["location"].startswith("/colony/cages/")
    assert 'class="back-link" href="/colony/#cages"' in detail.text
    assert "return_to=%2Fcolony%2F%23cages" in detail.text


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
    assert 'class="button button--quiet" href="#cages"' in html
    assert html.count('href="/?status=active&amp;view=all#cages"') >= 2
    assert 'href="/?status=active&amp;view=stock#cages"' in html


def test_cage_rows_expose_full_row_navigation_target(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        cage_id = client.app.state.database.create_cage(cage_card_id="ROW-LINK")
        response = client.get("/")

    assert response.status_code == 200
    row = _cage_row(response.text, cage_id)
    record_link = re.search(r'class="record-link" href="([^"]+)"', row)
    row_link = re.search(r'data-cage-row-href="([^"]+)"', row)
    assert record_link is not None and row_link is not None
    cage_href = unescape(record_link.group(1))
    assert unescape(row_link.group(1)) == cage_href
    assert urlsplit(cage_href).path == f"/cages/{cage_id}"
    assert parse_qs(urlsplit(cage_href).query)["return_to"] == ["/?view=all#cages"]


def test_cage_detail_returns_to_source_list_through_repeated_edits(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(
            cage_card_id="RETURN-SOURCE",
            animal_count=2,
            mouse_user="Alice",
            room="ROOM-REGULAR",
        )
        source = client.get(
            "/",
            params={
                "view": "stock",
                "search": "RETURN",
                "mouse_user": "Alice",
                "room": "ROOM-REGULAR",
                "status": "active",
                "sort": "cage_card_id",
                "direction": "desc",
            },
        )
        row = _cage_row(source.text, cage_id)
        record_link = re.search(r'class="record-link" href="([^"]+)"', row)
        assert record_link is not None
        detail_url = unescape(record_link.group(1))
        return_to = parse_qs(urlsplit(detail_url).query)["return_to"][0]
        detail = client.get(detail_url)

        back_link = re.search(r'class="back-link" href="([^"]+)"', detail.text)
        update_form = _form_tag(detail.text, f"/cages/{cage_id}/update")
        update_action = re.search(r'action="([^"]+)"', update_form)
        assert back_link is not None and update_action is not None
        first_update = client.post(
            unescape(update_action.group(1)),
            headers=_csrf(client),
            data={
                "cage_card_id": "RETURN-SOURCE",
                "room": "ROOM-REGULAR",
                "note": "first save",
            },
            follow_redirects=False,
        )
        second_update = client.post(
            unescape(update_action.group(1)),
            headers=_csrf(client),
            data={
                "cage_card_id": "RETURN-SOURCE",
                "room": "ROOM-REGULAR",
                "note": "second save",
            },
            follow_redirects=False,
        )
        refreshed = client.get(second_update.headers["location"])

    assert return_to == (
        "/?view=stock&search=RETURN&status=active&mouse_user=Alice&room=ROOM-REGULAR"
        "&sort=cage_card_id&direction=desc#cages"
    )
    assert unescape(back_link.group(1)) == return_to
    assert first_update.status_code == second_update.status_code == 303
    assert parse_qs(urlsplit(first_update.headers["location"]).query)["return_to"] == [
        return_to
    ]
    assert parse_qs(urlsplit(second_update.headers["location"]).query)["return_to"] == [
        return_to
    ]
    refreshed_back = re.search(r'class="back-link" href="([^"]+)"', refreshed.text)
    assert refreshed_back is not None
    assert unescape(refreshed_back.group(1)) == return_to


def test_cage_detail_rejects_unsafe_return_targets(tmp_path: Path) -> None:
    unsafe_targets = (
        "https://example.com/",
        "http://[",
        "//example.com/",
        "/cages/1",
        "/?view=invalid#cages",
        "/?view=all&view=stock#cages",
        "/?next=https%3A%2F%2Fexample.com#cages",
        "/?view=all#new-cage",
    )
    with _client(tmp_path) as client:
        cage_id = client.app.state.database.create_cage(cage_card_id="SAFE-RETURN")
        responses = [
            client.get(f"/cages/{cage_id}", params={"return_to": target})
            for target in unsafe_targets
        ]

    for response in responses:
        back_link = re.search(r'class="back-link" href="([^"]+)"', response.text)
        assert back_link is not None
        assert unescape(back_link.group(1)) == "/#cages"


def test_stock_and_using_views_render_separately_with_sex_breakdown(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        stock_id = database.create_cage(
            cage_card_id="VIEW-STOCK",
            animal_count=4,
            sex="M",
            genotype="StockHet",
        )
        stock_animals = database.list_animals(stock_id)
        database.update_animal(stock_animals[2]["id"], sex="F")
        database.update_animal(stock_animals[3]["id"], sex="U")
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

    assert re.search(rf'href="/cages/{stock_id}(?:\?[^\"]*)?"', stock_html)
    assert not re.search(rf'href="/cages/{using_id}(?:\?[^\"]*)?"', stock_html)
    assert not re.search(rf'href="/cages/{breeding_id}(?:\?[^\"]*)?"', stock_html)
    assert 'aria-label="2 male stock mice"' in stock_html
    assert 'aria-label="1 female stock mouse"' in stock_html
    assert "Unknown 1" in stock_html
    stock_heading = (
        '<span class="metric-card__label">Stock mice</span><span class="metric-card__hint">'
    )
    assert stock_heading in stock_html
    assert "view-switcher__label" not in stock_html
    assert re.search(r'<th scope="col">Sex</th>\s*<th scope="col">Genotype</th>', stock_html)
    assert 'aria-label="2 male mice"' in stock_row
    assert 'aria-label="1 female mouse"' in stock_row
    assert "Unknown 1" in stock_row
    assert '<td data-label="Genotype">StockHet</td>' in stock_row
    assert 'data-label="Details"' not in stock_row

    assert re.search(rf'href="/cages/{using_id}(?:\?[^\"]*)?"', using_html)
    assert not re.search(rf'href="/cages/{stock_id}(?:\?[^\"]*)?"', using_html)
    assert not re.search(rf'href="/cages/{breeding_id}(?:\?[^\"]*)?"', using_html)
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


def test_index_filters_sorts_and_preserves_extended_query(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        database.create_cage(
            cage_card_id="FILTER-A",
            animal_count=1,
            mouse_user="Alice",
            room="ROOM-REGULAR",
        )
        database.create_cage(
            cage_card_id="FILTER-B",
            animal_count=1,
            mouse_user="Alice",
            room="ROOM-REGULAR",
        )
        database.create_cage(
            cage_card_id="FILTER-C",
            animal_count=1,
            mouse_user="Bob",
            room="ROOM-REVERSE",
        )

        response = client.get(
            "/",
            params={
                "mouse_user": " Alice ",
                "room": "ROOM-REGULAR",
                "status": "active",
                "sort": "cage_card_id",
                "direction": "desc",
            },
        )
        invalid = client.get(
            "/",
            params={"status": "invalid", "sort": "invalid", "direction": "sideways"},
        )

    assert response.status_code == 200
    assert response.text.index("FILTER-B") < response.text.index("FILTER-A")
    assert "FILTER-C" not in response.text
    assert 'name="mouse_user"' in response.text
    assert 'value="Alice"' in response.text
    assert 'name="room"' in response.text
    assert 'value="ROOM-REGULAR"' in response.text

    for view_name, label in (
        ("all", "All cages"),
        ("stock", "Stock mice"),
        ("using", "In-use mice"),
        ("breeding", "Breeding pairs"),
    ):
        match = re.search(rf'href="([^"]+)"[^>]*>{re.escape(label)}</a>', response.text)
        assert match is not None
        assert parse_qs(urlsplit(unescape(match.group(1))).query) == {
            "view": [view_name],
            "status": ["active"],
            "mouse_user": ["Alice"],
            "room": ["ROOM-REGULAR"],
            "sort": ["cage_card_id"],
            "direction": ["desc"],
        }

    assert invalid.status_code == 200
    assert "FILTER-A" in invalid.text
    assert "FILTER-B" in invalid.text
    assert "FILTER-C" in invalid.text


def test_sortable_headers_cycle_ascending_descending_then_default(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        database.create_cage(cage_card_id="C-THIRD", status="inactive", room="Room B")
        database.create_cage(cage_card_id="A-FIRST", status="active", room="Room C")
        database.create_cage(cage_card_id="B-SECOND", status="on_order", room="Room A")
        database.create_cage(cage_card_id="D-NO-ROOM", status="active", room=None)
        database.create_cage(cage_card_id="E-ROOM-B", status="active", room="Room B")

        default_page = client.get("/?view=all")
        assert default_page.status_code == 200
        assert '<select name="sort">' not in default_page.text
        assert '<select name="direction">' not in default_page.text
        assert "Sort by" not in default_page.text

        for field in ("cage_card_id", "room", "status"):
            default_header, ascending_url = _sort_header(default_page.text, field)
            assert "aria-sort=" not in default_header
            assert parse_qs(urlsplit(ascending_url).query) == {
                "view": ["all"],
                "sort": [field],
                "direction": ["asc"],
            }

            ascending_page = client.get(ascending_url)
            ascending_header, descending_url = _sort_header(ascending_page.text, field)
            assert 'aria-sort="ascending"' in ascending_header
            assert "sort-link is-active" in ascending_header
            assert parse_qs(urlsplit(descending_url).query) == {
                "view": ["all"],
                "sort": [field],
                "direction": ["desc"],
            }

            descending_page = client.get(descending_url)
            descending_header, default_url = _sort_header(descending_page.text, field)
            assert 'aria-sort="descending"' in descending_header
            assert parse_qs(urlsplit(default_url).query) == {"view": ["all"]}

            restored_page = client.get(default_url)
            restored_header, restored_ascending_url = _sort_header(restored_page.text, field)
            assert "aria-sort=" not in restored_header
            assert "sort-link is-active" not in restored_header
            assert parse_qs(urlsplit(restored_ascending_url).query) == {
                "view": ["all"],
                "sort": [field],
                "direction": ["asc"],
            }

        cage_ascending = client.get("/?view=all&sort=cage_card_id&direction=asc")
        cage_descending = client.get("/?view=all&sort=cage_card_id&direction=desc")
        identifiers = ["A-FIRST", "B-SECOND", "C-THIRD", "D-NO-ROOM", "E-ROOM-B"]
        assert [cage_ascending.text.index(value) for value in identifiers] == sorted(
            cage_ascending.text.index(value) for value in identifiers
        )
        assert [cage_descending.text.index(value) for value in reversed(identifiers)] == sorted(
            cage_descending.text.index(value) for value in reversed(identifiers)
        )


def test_sort_headers_preserve_filters_and_explicit_sort_in_forms(tmp_path: Path) -> None:
    params = {
        "view": "stock",
        "search": "Alpha & Beta",
        "status": "active",
        "tag": "Group & One",
        "mouse_user": "Alice",
        "room": "Room A",
        "sort": "room",
        "direction": "desc",
    }
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(
            cage_card_id="Alpha & Beta",
            animal_count=2,
            mouse_user="Alice",
            room="Room A",
        )
        database.add_tag(cage_id, "Group & One")
        response = client.get("/", params=params)

    assert response.status_code == 200
    assert '<input type="hidden" name="sort" value="room">' in response.text
    assert '<input type="hidden" name="direction" value="desc">' in response.text
    room_header, default_url = _sort_header(response.text, "room")
    assert 'aria-sort="descending"' in room_header
    assert parse_qs(urlsplit(default_url).query) == {
        "view": ["stock"],
        "search": ["Alpha & Beta"],
        "status": ["active"],
        "tag": ["Group & One"],
        "mouse_user": ["Alice"],
        "room": ["Room A"],
    }


def test_mouse_user_flows_through_create_add_update_batch_and_wean(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        headers = _csrf(client)
        created_response = client.post(
            "/cages/new",
            headers=headers,
            data={
                "cage_card_id": "USER-SOURCE",
                "count": "1",
                "sex": "M",
                "dob": "2026-01-02",
                "genotype": "Founder",
                "mouse_user": "  Creator  ",
                "room": "ROOM-BREEDING",
                "is_breeding_pair": "on",
            },
            follow_redirects=False,
        )
        cage_id = int(urlsplit(created_response.headers["location"]).path.rsplit("/", 1)[1])
        database = client.app.state.database
        created_animal = database.list_animals(cage_id)[0]

        added_response = client.post(
            f"/cages/{cage_id}/add-mice",
            headers=headers,
            data={
                "count": "1",
                "sex": "F",
                "dob": "2026-02-03",
                "genotype": "Added",
                "mouse_user": "Adder",
            },
            follow_redirects=False,
        )
        after_add = database.list_animals(cage_id)
        added_animal = next(
            animal for animal in after_add if animal["id"] != created_animal["id"]
        )

        updated_response = client.post(
            f"/animals/{created_animal['id']}/update",
            headers=headers,
            data={
                "legacy_id": "Legacy user mouse",
                "sex": "M",
                "dob": "2026-01-02",
                "genotype": "Founder",
                "mouse_user": "Editor",
                "note": "updated owner",
            },
            follow_redirects=False,
        )
        edited_animal = database.get_animal(int(created_animal["id"]))

        batch_response = client.post(
            f"/cages/{cage_id}/animals/batch-update",
            headers=headers,
            data={"field": "mouse_user", "value": "Batch Owner"},
            follow_redirects=False,
        )
        after_batch = database.list_animals(cage_id, include_inactive=True)

        wean_response = client.post(
            f"/cages/{cage_id}/wean",
            headers=headers,
            data={
                "count": "2",
                "sex": "F",
                "dob": "2026-07-01",
                "genotype": "Weaned",
                "mouse_user": "Wean User",
                "destination_cage_card_id": "USER-WEAN",
            },
            follow_redirects=False,
        )
        wean_id = int(urlsplit(wean_response.headers["location"]).path.rsplit("/", 1)[1])
        weaned_animals = database.list_animals(wean_id)

    assert created_response.status_code == 303
    assert created_animal["mouse_user"] == "Creator"
    assert added_response.status_code == 303
    assert added_animal["mouse_user"] == "Adder"
    assert updated_response.status_code == 303
    assert edited_animal is not None and edited_animal["mouse_user"] == "Editor"
    assert batch_response.status_code == 303
    assert {animal["mouse_user"] for animal in after_batch} == {"Batch Owner"}
    assert wean_response.status_code == 303
    assert len(weaned_animals) == 2
    assert {animal["mouse_user"] for animal in weaned_animals} == {"Wean User"}


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
        cage_id = database.create_cage(
            cage_card_id="ADJUST-MICE",
            animal_count=1,
            is_breeding_pair=True,
        )
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


def test_direct_add_mice_is_limited_to_breeding_pair_cages(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        regular_id = database.create_cage(cage_card_id="REGULAR-CAGE", animal_count=1)
        breeding_id = database.create_cage(
            cage_card_id="BREEDING-CAGE",
            animal_count=1,
            is_breeding_pair=True,
        )
        inactive_breeding_id = database.create_cage(
            cage_card_id="INACTIVE-BREEDING-CAGE",
            status="inactive",
            animal_count=1,
            is_breeding_pair=True,
        )

        regular_detail = client.get(f"/cages/{regular_id}")
        breeding_detail = client.get(f"/cages/{breeding_id}")
        inactive_breeding_detail = client.get(f"/cages/{inactive_breeding_id}")
        headers = _csrf(client)
        rejected = client.post(
            f"/cages/{regular_id}/add-mice",
            headers=headers,
            data={"count": "1", "sex": "F"},
            follow_redirects=False,
        )
        accepted = client.post(
            f"/cages/{breeding_id}/add-mice",
            headers=headers,
            data={"count": "1", "sex": "F"},
            follow_redirects=False,
        )

        regular_animals = database.list_animals(regular_id)
        breeding_animals = database.list_animals(breeding_id)

    regular_action = f'/cages/{regular_id}/add-mice'
    breeding_action = f'/cages/{breeding_id}/add-mice'
    assert regular_detail.status_code == 200
    assert regular_action not in regular_detail.text
    assert f'/cages/{regular_id}/split' in regular_detail.text
    assert breeding_detail.status_code == 200
    assert breeding_action in breeding_detail.text
    assert inactive_breeding_detail.status_code == 200
    assert f'/cages/{inactive_breeding_id}/add-mice' not in inactive_breeding_detail.text

    assert rejected.status_code == 303
    rejected_location = urlsplit(rejected.headers["location"])
    assert rejected_location.path == f"/cages/{regular_id}"
    assert parse_qs(rejected_location.query)["kind"] == ["error"]
    assert parse_qs(rejected_location.query)["message"] == [
        "Mice can only be added directly to breeding-pair cages."
    ]
    assert len(regular_animals) == 1

    assert accepted.status_code == 303
    assert len(breeding_animals) == 2


def test_batch_update_cage_animals_changes_all_records_and_returns_to_mice(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(
            cage_card_id="BATCH-ROUTE",
            animal_count=3,
            sex="M",
            genotype="Original",
        )
        inactive_id = int(database.list_animals(cage_id)[0]["id"])
        database.toggle_animal(inactive_id)
        headers = _csrf(client)

        response = client.post(
            f"/cages/{cage_id}/animals/batch-update",
            headers=headers,
            data={"field": "genotype", "value": "  WT  "},
            follow_redirects=False,
        )
        updated = database.list_animals(cage_id, include_inactive=True)
        invalid = client.post(
            f"/cages/{cage_id}/animals/batch-update",
            headers=headers,
            data={"field": "note", "value": "not allowed"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    response_location = urlsplit(response.headers["location"])
    assert response_location.path == f"/cages/{cage_id}"
    assert response_location.fragment == "mice"
    assert parse_qs(response_location.query)["message"] == [
        "Updated genotype for 3 mice."
    ]
    assert {animal["genotype"] for animal in updated} == {"WT"}
    assert {animal["status"] for animal in updated} == {"active", "inactive"}

    assert invalid.status_code == 303
    invalid_location = urlsplit(invalid.headers["location"])
    assert invalid_location.fragment == "mice"
    assert parse_qs(invalid_location.query)["kind"] == ["error"]
    assert parse_qs(invalid_location.query)["message"] == [
        "Choose sex, genotype, date of birth, or mouse user."
    ]


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

    post_actions = re.findall(
        r'<form\b[^>]*\bmethod="post"[^>]*\baction="([^"]+)"',
        html,
    )
    assert post_actions
    assert all("return_to=%2F%23cages" in action for action in post_actions)

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

    batch_action = f"/cages/{cage_id}/animals/batch-update"
    batch_forms = re.findall(
        rf'<form\b[^>]*action="{re.escape(batch_action)}(?:\?[^\"]*)?"[^>]*>',
        html,
    )
    assert len(batch_forms) == 4
    assert all('data-return-hash="#mice"' in form for form in batch_forms)
    batch_form_bodies = re.findall(
        r'<form\b[^>]*action="[^"]*/animals/batch-update(?:\?[^\"]*)?"[^>]*>'
        r".*?</form>",
        html,
        re.DOTALL,
    )
    mouse_user_batch = next(
        form for form in batch_form_bodies if 'value="mouse_user"' in form
    )
    mouse_user_value = re.search(
        r'<input[^>]*name="value"[^>]*>',
        mouse_user_batch,
    )
    assert mouse_user_value is not None
    assert "required" not in mouse_user_value.group(0)
    assert 'placeholder="Leave blank to clear"' in mouse_user_value.group(0)

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
