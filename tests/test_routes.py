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
    assert "No login required" in root.text
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

        normal_id = database.create_cage(
            cage_card_id="BREED-UPDATE",
            room="ROOM-REGULAR",
            protocol="KEEP-ME",
        )
        update_response = client.post(
            f"/cages/{normal_id}/update",
            headers=_csrf(client),
            data={
                "room": "ROOM-REGULAR",
                "protocol": "MUST-NOT-REPLACE",
                "note": "breeding pair",
                "is_breeding_pair": "on",
            },
            follow_redirects=False,
        )
        updated = database.get_cage(normal_id)

    assert update_response.status_code == 303
    assert updated is not None
    assert updated["is_breeding_pair"] is True
    assert updated["room"] == "ROOM-REGULAR"
    assert updated["protocol"] == "KEEP-ME"


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
    ):
        assert f'data-return-hash="{mouse_hash}"' in _form_tag(html, action)
