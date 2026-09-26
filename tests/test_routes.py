from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import LOGIN_ANSWER_PLACEHOLDER, Settings
from app.database import Database
from app.main import create_app
from app.reconciliation import MAX_AOPS_CSV_BYTES
from app.security import AOPS_UPLOAD_REQUEST_MAX_BYTES, LOGIN_COOKIE_NAME

BASE_URL = "http://127.0.0.1:8765"
TEST_LOGIN_ANSWER = "test-only-login-answer"
TEST_INTEGRATION_TOKEN = "test-only-integration-token-0123456789abcdef"
TEST_ROOM_ALIASES = {
    "ROOM-REGULAR": "Regular Cycle room",
    "ROOM-REVERSE": "Reverse Cycle room",
    "ROOM-BREEDING": "Breeding Core",
}
TEST_BREEDING_ROOMS = {"ROOM-BREEDING", "Breeding Core"}


def _client(
    tmp_path: Path,
    *,
    root_path: str = "",
    authenticated: bool = True,
    integration_token: str | None = None,
) -> TestClient:
    database = Database(
        tmp_path / "route-test.db",
        room_aliases=TEST_ROOM_ALIASES,
        breeding_rooms=TEST_BREEDING_ROOMS,
    )
    app = create_app(
        settings=Settings(
            database_path=tmp_path / "route-test.db",
            photo_directory=tmp_path / "photos",
            seed_on_empty=False,
            root_path=root_path,
            login_answer=TEST_LOGIN_ANSWER,
            integration_token=integration_token,
        ),
        database=database,
        run_seed=False,
    )
    client = TestClient(app, base_url=BASE_URL, client=("127.0.0.1", 50_000))
    if authenticated:
        client.cookies.set(
            LOGIN_COOKIE_NAME,
            app.state.login_manager.issue_session_token(),
        )
    return client


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


def _aops_csv(*rows: tuple[str, str, str, str, str, str, str]) -> bytes:
    header = "Cage Card ID,Status,# Animals,Room,Protocol,On Census Date,Off Census Date"
    return ("\n".join((header, *(",".join(row) for row in rows))) + "\n").encode()


def _colony_snapshot(database: Database) -> tuple[list[tuple[object, ...]], ...]:
    return tuple(
        [tuple(row) for row in database.connection.execute(query).fetchall()]
        for query in (
            "SELECT * FROM cages ORDER BY id",
            "SELECT * FROM animals ORDER BY id",
            "SELECT * FROM movements ORDER BY id",
        )
    )


def test_public_login_answer_placeholder_is_rejected() -> None:
    with pytest.raises(ValidationError, match="MOUSELINE_LOGIN_ANSWER"):
        Settings(login_answer=LOGIN_ANSWER_PLACEHOLDER)


def test_read_only_integration_api_uses_separate_bearer_token(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        authenticated=False,
        integration_token=TEST_INTEGRATION_TOKEN,
    ) as client:
        cage_id = client.app.state.database.create_cage(
            cage_card_id="API-CAGE",
            animal_count=1,
            sex="F",
            dob="2026-01-02",
            genotype="Ai32/WT",
            mouse_user="Planner",
            room="ROOM-REGULAR",
            protocol="PROTO-1",
        )
        animal = client.app.state.database.list_animals(cage_id)[0]
        client.app.state.database.update_animal(
            int(animal["id"]),
            legacy_id="legacy-42",
        )

        missing = client.get("/api/v1/animals/resolve", params={"identifier": animal["public_id"]})
        wrong = client.get(
            "/api/v1/animals/resolve",
            params={"identifier": animal["public_id"]},
            headers={"Authorization": "Bearer definitely-not-the-token"},
        )
        headers = {"Authorization": f"Bearer {TEST_INTEGRATION_TOKEN}"}
        health = client.get("/api/v1/health", headers=headers)
        response = client.get(
            "/api/v1/animals/resolve",
            params={"identifier": "legacy-42"},
            headers=headers,
        )

    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["code"] == wrong.json()["code"] == "invalid_token"
    assert health.status_code == 200
    assert health.json()["capabilities"] == ["subject.read"]
    assert response.status_code == 200
    payload = response.json()
    assert payload["matchedBy"] == "legacyId"
    assert payload["queryIdentifier"] == "legacy-42"
    assert payload["subject"]["publicId"] == animal["public_id"]
    assert payload["subject"]["legacyId"] == "legacy-42"
    assert payload["subject"]["cage"]["cageCardId"] == "API-CAGE"
    assert payload["subject"]["surgeries"] == []
    assert payload["usableForNavigation"] is False
    receipt_material = {
        "schemaVersion": 1,
        "service": "mouse_line",
        "subject": payload["subject"],
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            receipt_material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert payload["subjectRecordSha256"] == expected_digest


def test_integration_api_disabled_not_redirected_to_login(tmp_path: Path) -> None:
    with _client(tmp_path, authenticated=False) as client:
        response = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {TEST_INTEGRATION_TOKEN}"},
            follow_redirects=False,
        )

    assert response.status_code == 503
    assert response.json()["code"] == "integration_disabled"
    assert "location" not in response.headers


def test_integration_api_requires_exact_unambiguous_identifier(tmp_path: Path) -> None:
    with _client(
        tmp_path,
        authenticated=False,
        integration_token=TEST_INTEGRATION_TOKEN,
    ) as client:
        first_cage = client.app.state.database.create_cage(
            cage_card_id="API-DUP-1",
            animal_count=1,
        )
        second_cage = client.app.state.database.create_cage(
            cage_card_id="API-DUP-2",
            animal_count=1,
        )
        first = client.app.state.database.list_animals(first_cage)[0]
        second = client.app.state.database.list_animals(second_cage)[0]
        client.app.state.database.update_animal(int(first["id"]), legacy_id="duplicate")
        client.app.state.database.update_animal(int(second["id"]), legacy_id="duplicate")
        headers = {"Authorization": f"Bearer {TEST_INTEGRATION_TOKEN}"}

        ambiguous = client.get(
            "/api/v1/animals/resolve",
            params={"identifier": "duplicate"},
            headers=headers,
        )
        wrong_case = client.get(
            "/api/v1/animals/resolve",
            params={"identifier": str(first["public_id"]).lower()},
            headers=headers,
        )
        padded = client.get(
            "/api/v1/animals/resolve",
            params={"identifier": f" {first['public_id']} "},
            headers=headers,
        )

    assert ambiguous.status_code == 409
    assert ambiguous.json()["code"] == "ambiguous_legacy_id"
    assert wrong_case.status_code == 404
    assert wrong_case.json()["code"] == "subject_not_found"
    assert padded.status_code == 422
    assert padded.json()["code"] == "invalid_identifier"


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
        csrf_headers = _csrf(client)
        invalid_origin = client.post(
            "/cages/new",
            data={"count": "0", "sex": "U"},
            headers={**csrf_headers, "Origin": "http://127.0.0.1:9999"},
        )
        cross_site = client.post(
            "/cages/new",
            data={"count": "0", "sex": "U"},
            headers={**csrf_headers, "Sec-Fetch-Site": "cross-site"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "invalid_csrf"
    assert invalid_origin.status_code == 403
    assert invalid_origin.json()["code"] == "invalid_origin"
    assert cross_site.status_code == 403
    assert cross_site.json()["code"] == "invalid_origin"


def test_login_page_protects_every_application_route(tmp_path: Path) -> None:
    with _client(tmp_path, authenticated=False) as client:
        root = client.get("/", follow_redirects=False)
        health = client.get("/healthz", follow_redirects=False)
        cage = client.get("/cages/999", follow_redirects=False)
        login = client.get("/login")
        stylesheet = client.get("/static/styles.css")
        script = client.get("/static/app.js")

    assert root.status_code == health.status_code == cage.status_code == 303
    assert root.headers["location"] == "/login?next=%2F"
    assert health.headers["location"] == "/login?next=%2Fhealthz"
    assert cage.headers["location"] == "/login?next=%2Fcages%2F999"
    assert login.status_code == 200
    assert "What's your PI's first name?" in login.text
    assert 'type="password" name="answer"' in login.text
    assert TEST_LOGIN_ANSWER not in login.text
    assert "app.js" not in login.text
    assert stylesheet.status_code == script.status_code == 200
    assert TEST_LOGIN_ANSWER not in f"{stylesheet.text}{script.text}"


def test_login_normalizes_answer_sets_secure_cookie_and_logout_clears_it(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, authenticated=False) as client:
        incorrect = client.post(
            "/login",
            data={"answer": "not the answer"},
            follow_redirects=False,
        )
        accepted = client.post(
            "/login",
            data={"answer": f"  {TEST_LOGIN_ANSWER.upper()}  ", "next": "/?view=stock"},
            follow_redirects=False,
        )
        root = client.get("/")
        health = client.get("/healthz")
        issued_session = client.cookies.get(LOGIN_COOKIE_NAME)
        assert issued_session is not None
        token = re.search(r'<meta name="csrf-token" content="([^"]+)">', root.text)
        assert token is not None
        logout = client.post(
            "/logout",
            headers={"Origin": BASE_URL, "X-CSRF-Token": token.group(1)},
            follow_redirects=False,
        )
        locked_again = client.get("/", follow_redirects=False)
        client.cookies.set(LOGIN_COOKIE_NAME, issued_session)
        replayed_session = client.get("/", follow_redirects=False)

    assert incorrect.status_code == 401
    assert "That answer is not correct." in incorrect.text
    assert LOGIN_COOKIE_NAME not in incorrect.headers.get("set-cookie", "")
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/?view=stock"
    cookie = accepted.headers["set-cookie"].casefold()
    assert f"{LOGIN_COOKIE_NAME}=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "max-age=2592000" in cookie
    assert root.status_code == 200
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    assert locked_again.status_code == 303
    assert replayed_session.status_code == 303


def test_login_session_survives_application_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "persistent-login.db"
    settings = Settings(
        database_path=database_path,
        seed_on_empty=False,
        login_answer=TEST_LOGIN_ANSWER,
    )

    first_database = Database(database_path)
    first_app = create_app(
        settings=settings,
        database=first_database,
        run_seed=False,
    )
    with TestClient(
        first_app,
        base_url=BASE_URL,
        client=("127.0.0.1", 50_000),
    ) as first_client:
        accepted = first_client.post(
            "/login",
            headers={"Origin": BASE_URL},
            data={"answer": TEST_LOGIN_ANSWER},
            follow_redirects=False,
        )
        issued_session = first_client.cookies.get(LOGIN_COOKIE_NAME)

    assert accepted.status_code == 303
    assert issued_session is not None

    second_database = Database(database_path)
    second_app = create_app(
        settings=settings,
        database=second_database,
        run_seed=False,
    )
    with TestClient(
        second_app,
        base_url=BASE_URL,
        client=("127.0.0.1", 50_001),
    ) as second_client:
        second_client.cookies.set(LOGIN_COOKIE_NAME, issued_session)
        root = second_client.get("/")

    assert root.status_code == 200


def test_login_rejects_external_return_url_and_prefixes_reverse_proxy_paths(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, root_path="/colony/", authenticated=False) as client:
        gated = client.get("/", follow_redirects=False)
        rejected_logins = [
            client.get("/login", params={"next": target})
            for target in (
                "https://example.com/",
                "//example.com/",
                "/colony/%5C%5Cexample.com/",
            )
        ]
        accepted = client.post(
            "/login",
            headers={"Origin": BASE_URL},
            data={"answer": TEST_LOGIN_ANSWER, "next": "https://example.com/"},
            follow_redirects=False,
        )

    assert gated.headers["location"] == "/colony/login?next=%2Fcolony%2F"
    assert all('action="/colony/login"' in login.text for login in rejected_logins)
    assert all('name="next" value="/colony/"' in login.text for login in rejected_logins)
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/colony/"
    assert "path=/colony" in accepted.headers["set-cookie"].casefold()


def test_root_and_health_are_available_after_login(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        root = client.get("/")
        health = client.get("/healthz")
    assert root.status_code == 200
    assert 'action="/logout"' in root.text
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


def test_default_cage_list_hides_inactive_and_shows_last_changed_date(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        active_id = database.create_cage(cage_card_id="DEFAULT-ACTIVE")
        inactive_id = database.create_cage(
            cage_card_id="DEFAULT-INACTIVE",
            status="inactive",
        )
        on_order_id = database.create_cage(
            cage_card_id="DEFAULT-ON-ORDER",
            status="on_order",
        )
        database.connection.execute(
            "UPDATE cages SET updated_at = '2024-05-06 12:34:56' WHERE id = ?",
            (active_id,),
        )

        default_page = client.get("/")
        all_page = client.get("/", params={"status": "all"})

    assert default_page.status_code == all_page.status_code == 200
    assert re.search(rf'href="/cages/{active_id}(?:\?[^"]*)?"', default_page.text)
    assert not re.search(rf'href="/cages/{inactive_id}(?:\?[^"]*)?"', default_page.text)
    assert not re.search(rf'href="/cages/{on_order_id}(?:\?[^"]*)?"', default_page.text)
    assert '<option value="active" selected>Active</option>' in default_page.text
    active_row = _cage_row(default_page.text, active_id)
    assert '<th scope="col">Last changed</th>' in default_page.text
    assert (
        '<td class="last-changed-cell" data-label="Last changed">'
        '<time datetime="2024-05-06">2024-05-06</time></td>'
    ) in active_row

    assert '<option value="all" selected>All statuses</option>' in all_page.text
    for cage_id in (active_id, inactive_id, on_order_id):
        assert re.search(rf'href="/cages/{cage_id}(?:\?[^"]*)?"', all_page.text)
    all_row = _cage_row(all_page.text, inactive_id)
    record_link = re.search(r'class="record-link" href="([^"]+)"', all_row)
    assert record_link is not None
    all_return_to = parse_qs(urlsplit(unescape(record_link.group(1))).query)["return_to"]
    assert all_return_to == ["/?view=all&status=all#cages"]


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
        "/?view=stock&search=RETURN&mouse_user=Alice&room=ROOM-REGULAR"
        "&sort=cage_card_id&direction=desc#cages"
    )
    assert unescape(back_link.group(1)) == return_to
    assert first_update.status_code == second_update.status_code == 303
    assert parse_qs(urlsplit(first_update.headers["location"]).query)["return_to"] == [return_to]
    assert parse_qs(urlsplit(second_update.headers["location"]).query)["return_to"] == [return_to]
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

        default_page = client.get("/?view=all&status=all")
        assert default_page.status_code == 200
        assert '<select name="sort">' not in default_page.text
        assert '<select name="direction">' not in default_page.text
        assert "Sort by" not in default_page.text

        for field in ("cage_card_id", "room", "status"):
            default_header, ascending_url = _sort_header(default_page.text, field)
            assert "aria-sort=" not in default_header
            assert parse_qs(urlsplit(ascending_url).query) == {
                "view": ["all"],
                "status": ["all"],
                "sort": [field],
                "direction": ["asc"],
            }

            ascending_page = client.get(ascending_url)
            ascending_header, descending_url = _sort_header(ascending_page.text, field)
            assert 'aria-sort="ascending"' in ascending_header
            assert "sort-link is-active" in ascending_header
            assert parse_qs(urlsplit(descending_url).query) == {
                "view": ["all"],
                "status": ["all"],
                "sort": [field],
                "direction": ["desc"],
            }

            descending_page = client.get(descending_url)
            descending_header, default_url = _sort_header(descending_page.text, field)
            assert 'aria-sort="descending"' in descending_header
            assert parse_qs(urlsplit(default_url).query) == {
                "view": ["all"],
                "status": ["all"],
            }

            restored_page = client.get(default_url)
            restored_header, restored_ascending_url = _sort_header(restored_page.text, field)
            assert "aria-sort=" not in restored_header
            assert "sort-link is-active" not in restored_header
            assert parse_qs(urlsplit(restored_ascending_url).query) == {
                "view": ["all"],
                "status": ["all"],
                "sort": [field],
                "direction": ["asc"],
            }

        cage_ascending = client.get("/?view=all&status=all&sort=cage_card_id&direction=asc")
        cage_descending = client.get("/?view=all&status=all&sort=cage_card_id&direction=desc")
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
        added_animal = next(animal for animal in after_add if animal["id"] != created_animal["id"])

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

    regular_action = f"/cages/{regular_id}/add-mice"
    breeding_action = f"/cages/{breeding_id}/add-mice"
    assert regular_detail.status_code == 200
    assert regular_action not in regular_detail.text
    assert f"/cages/{regular_id}/split" not in regular_detail.text
    assert breeding_detail.status_code == 200
    assert breeding_action in breeding_detail.text
    assert inactive_breeding_detail.status_code == 200
    assert f"/cages/{inactive_breeding_id}/add-mice" not in inactive_breeding_detail.text

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


def test_split_and_surgery_controls_follow_active_mouse_count(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        single_id = database.create_cage(cage_card_id="SINGLE-CONTROLS", animal_count=1)
        multi_id = database.create_cage(cage_card_id="MULTI-CONTROLS", animal_count=2)
        single_animal = database.list_animals(single_id)[0]
        multi_animal = database.list_animals(multi_id)[0]
        database.add_surgery(
            multi_animal["id"],
            surgery_date="2026-07-01",
            surgery_time=None,
            operator="Operator",
            surgery_type="Headplate",
        )

        single = client.get(f"/cages/{single_id}")
        multi = client.get(f"/cages/{multi_id}")

    assert f"/cages/{single_id}/split" not in single.text
    assert f"/animals/{single_animal['id']}/surgery" in single.text
    assert "animal-selector" not in single.text
    assert f"/cages/{multi_id}/split" in multi.text
    assert "animal-selector" in multi.text
    assert "surgery-block" not in multi.text
    assert f"/animals/{multi_animal['id']}/surgery" not in multi.text


def test_room_changes_use_selects_and_new_records_get_date_defaults(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(
            cage_card_id="ROOM-AND-DATES",
            animal_count=1,
            room="ROOM-REGULAR",
            is_breeding_pair=True,
        )
        root = client.get("/")
        detail = client.get(f"/cages/{cage_id}")

        wean_response = client.post(
            f"/cages/{cage_id}/wean",
            headers=_csrf(client),
            data={
                "count": "1",
                "sex": "U",
                "destination_cage_card_id": "AUTO-WEAN-DOB",
                "destination_room": "ROOM-REVERSE",
            },
            follow_redirects=False,
        )
        wean_id = int(urlsplit(wean_response.headers["location"]).path.rsplit("/", 1)[1])
        weaned = database.list_animals(wean_id)
        wean_cage = database.get_cage(wean_id)

    today = date.today().isoformat()
    wean_dob = (date.today() - timedelta(days=21)).isoformat()
    assert '<select name="room">' in root.text
    assert '<input type="text" name="room"' not in root.text
    assert '<select name="room">' in detail.text
    assert detail.text.count('<select name="destination_room">') == 1
    assert 'value="ROOM-REGULAR" selected' in detail.text
    assert 'value="ROOM-REVERSE"' in detail.text
    assert f'name="surgery_date" value="{today}"' in detail.text
    assert f'name="dob" value="{wean_dob}" required' in detail.text
    assert wean_response.status_code == 303
    assert {animal["dob"] for animal in weaned} == {wean_dob}
    assert wean_cage is not None and wean_cage["room"] == "ROOM-REVERSE"


def test_batch_update_cage_animals_changes_all_records_and_returns_to_mice(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(
            cage_card_id="BATCH-ROUTE",
            animal_count=3,
            sex="M",
            dob="2026-01-02",
            genotype="Original",
            mouse_user="Original owner",
        )
        inactive_id = int(database.list_animals(cage_id)[0]["id"])
        database.toggle_animal(inactive_id)
        headers = _csrf(client)

        response = client.post(
            f"/cages/{cage_id}/animals/batch-update",
            headers=headers,
            data={
                "batch_mode": "selected",
                "change_sex": "true",
                "change_genotype": "true",
                "change_dob": "true",
                "change_mouse_user": "true",
                "sex": "F",
                "genotype": "  WT  ",
                "dob": "2026-02-03",
                "mouse_user": "Batch owner",
            },
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
    assert parse_qs(response_location.query)["message"] == ["Updated 4 properties for 3 mice."]
    assert {animal["genotype"] for animal in updated} == {"WT"}
    assert {animal["sex"] for animal in updated} == {"F"}
    assert {animal["dob"] for animal in updated} == {"2026-02-03"}
    assert {animal["mouse_user"] for animal in updated} == {"Batch owner"}
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


def test_remove_surgery_route_deletes_record_and_returns_to_mouse(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(cage_card_id="REMOVE-SURGERY", animal_count=1)
        animal_id = int(database.list_animals(cage_id)[0]["id"])
        surgery_id = database.add_surgery(
            animal_id,
            surgery_date="2026-07-01",
            surgery_time=None,
            operator="Operator",
            surgery_type="Headplate",
        )
        assert surgery_id is not None

        response = client.post(
            f"/surgeries/{surgery_id}/remove",
            headers=_csrf(client),
            follow_redirects=False,
        )
        removed = database.get_surgery(surgery_id)
        missing = client.post(
            "/surgeries/999999/remove",
            headers=_csrf(client),
        )

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == f"/cages/{cage_id}"
    assert parse_qs(location.query)["message"] == ["Surgery record removed."]
    assert removed is None
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
    cage_actions = [
        action for action in post_actions if action not in {"/logout", "/photos/recognize"}
    ]
    assert cage_actions
    assert all("return_to=%2F%23cages" in action for action in cage_actions)
    assert "/logout" in post_actions

    for action in (
        f"/cages/{cage_id}/toggle",
        f"/cages/{cage_id}/update",
        f"/cages/{cage_id}/tags",
        f"/cages/{cage_id}/tags/{tag_id}/remove",
    ):
        assert 'data-return-hash="#cage-details"' in _form_tag(html, action)

    for action in (
        f"/cages/{cage_id}/add-mice",
        f"/cages/{cage_id}/wean",
    ):
        assert 'data-return-hash="#cage-actions"' in _form_tag(html, action)
    assert f"/cages/{cage_id}/split" not in html

    batch_action = f"/cages/{cage_id}/animals/batch-update"
    batch_forms = re.findall(
        rf'<form\b[^>]*action="{re.escape(batch_action)}(?:\?[^\"]*)?"[^>]*>',
        html,
    )
    assert len(batch_forms) == 1
    assert all('data-return-hash="#mice"' in form for form in batch_forms)
    batch_form_bodies = re.findall(
        r'<form\b[^>]*action="[^"]*/animals/batch-update(?:\?[^\"]*)?"[^>]*>'
        r".*?</form>",
        html,
        re.DOTALL,
    )
    assert len(batch_form_bodies) == 1
    batch_form = batch_form_bodies[0]
    for property_name in ("sex", "genotype", "dob", "mouse_user"):
        assert f'name="change_{property_name}"' in batch_form
        assert f'name="{property_name}"' in batch_form
    assert batch_form.count("Save changes") == 1

    for action in (
        f"/animals/{animal['id']}/surgery",
        f"/animals/{animal['id']}/update",
        f"/animals/{animal['id']}/toggle",
        f"/surgeries/{surgery_id}/update",
        f"/surgeries/{surgery_id}/remove",
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


def test_aops_review_is_third_workspace_tab_and_prefixes_root_path_urls(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, root_path="/colony/") as client:
        root = client.get("/")
        primary_nav = re.search(
            r'<nav class="primary-nav".*?</nav>',
            root.text,
            flags=re.DOTALL,
        )
        assert primary_nav is not None
        tabs = re.findall(
            r'data-workspace-tab-link="[^"]+">([^<]+)</a>',
            primary_nav.group(0),
        )

        analyzed = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={
                "csv_file": (
                    "official.csv",
                    _aops_csv(("CC01000001", "Active", "1", "", "", "", "")),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
        location = urlsplit(analyzed.headers["location"])
        reconciliation_id = int(parse_qs(location.query)["reconciliation"][0])
        review = client.get(analyzed.headers["location"])

    assert root.status_code == 200
    assert tabs == ["Cages", "Create cage", "Update from CSV"]
    assert (
        'id="aops-review" class="workspace-view" role="tabpanel" '
        'aria-labelledby="workspace-tab-aops-review"' in root.text
    )
    assert 'href="/colony/#aops-review"' in root.text
    upload_form = _form_tag(root.text, "/colony/aops-reconcile/analyze")
    assert 'method="post"' in upload_form
    assert 'enctype="multipart/form-data"' in upload_form
    assert 'data-return-hash="#aops-review"' in upload_form
    assert 'type="file" name="csv_file" accept=".csv,text/csv"' in root.text
    assert analyzed.status_code == 303
    assert location.path == "/colony/"
    assert location.fragment == "aops-review"
    assert review.status_code == 200
    assert f'action="/colony/aops-reconcile/{reconciliation_id}/apply-all"' in review.text
    assert f'action="/colony/aops-reconcile/{reconciliation_id}/keep-all"' in review.text


def test_csv_review_survives_navigation_and_shows_saved_results(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        assert database.get_latest_aops_reconciliation() is None
        payload = _aops_csv(("CC01000001", "Active", "2", "ROOM-REGULAR", "", "", ""))
        uploaded = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={"csv_file": ("nu-export.csv", payload, "text/csv")},
            follow_redirects=False,
        )
        run_id = int(parse_qs(urlsplit(uploaded.headers["location"]).query)["reconciliation"][0])
        resumed = client.get("/?status=active")
        assert f'data-aops-reconciliation-id="{run_id}"' in resumed.text
        assert "2. Review and apply" in resumed.text
        assert "Apply all 1 update" in resumed.text
        assert database.list_cages(status=None) == []

        applied = client.post(f"/aops-reconcile/{run_id}/apply-all", headers=_csrf(client))
        assert "CSV update complete: 1 update applied" in applied.text
        saved = client.get("/")
        assert "Updates saved" in saved.text
        assert "2. Review and apply" not in saved.text
        snapshot = _colony_snapshot(database)

        repeated = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={"csv_file": ("nu-export-again.csv", payload, "text/csv")},
            follow_redirects=False,
        )
        latest_id = int(parse_qs(urlsplit(repeated.headers["location"]).query)["reconciliation"][0])
        assert latest_id > run_id
        latest = client.get("/")
        assert f'data-aops-reconciliation-id="{latest_id}"' in latest.text
        assert "Mouse Line matches this AOPS export" in latest.text
        assert _colony_snapshot(database) == snapshot
        older = client.get(f"/?reconciliation={run_id}")
        assert f'data-aops-reconciliation-id="{run_id}"' in older.text
        assert "Updates saved" in older.text


def test_invalid_csv_does_not_show_previous_preview_as_new_upload(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={
                "csv_file": (
                    "valid.csv",
                    _aops_csv(("CC01000001", "Active", "2", "", "", "", "")),
                    "text/csv",
                )
            },
        )
        latest = database.get_latest_aops_reconciliation()
        assert latest is not None
        before = _colony_snapshot(database)
        rejected = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={"csv_file": ("invalid.csv", b"incorrect header", "text/csv")},
        )
        assert "missing required columns" in rejected.text
        assert "data-aops-results" not in rejected.text
        assert database.get_latest_aops_reconciliation() == latest
        assert _colony_snapshot(database) == before
        resumed = client.get("/")
        assert f'data-aops-reconciliation-id="{latest["id"]}"' in resumed.text


def test_aops_analyze_stages_differences_and_renders_escaped_review(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        database.create_cage(cage_card_id="CC11000001", animal_count=1)
        database.create_cage(cage_card_id="CC11000002", animal_count=2)
        database.create_cage(cage_card_id="CC11000003", animal_count=1)
        database.create_cage(cage_card_id="CC11000005", animal_count=2)
        before = _colony_snapshot(database)

        analyzed = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={
                "csv_file": (
                    "official & <unsafe>.csv",
                    _aops_csv(
                        ("CC11000001", "Active", "3", "", "", "", ""),
                        ("CC11000002", "Deactivated", "2", "", "", "", ""),
                        ("CC11000003", "Active", "1", "", "", "", ""),
                        ("CC11000004", "Active", "2", "", "", "", ""),
                        ("CC11000005", "Active", "1", "", "", "", ""),
                    ),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )

        location = urlsplit(analyzed.headers["location"])
        query = parse_qs(location.query)
        reconciliation_id = int(query["reconciliation"][0])
        run = database.get_aops_reconciliation(reconciliation_id)
        review = client.get(f"/?reconciliation={reconciliation_id}")
        after = _colony_snapshot(database)

    assert analyzed.status_code == 303
    assert location.path == "/"
    assert location.fragment == "aops-review"
    assert query["kind"] == ["success"]
    assert "No colony data changed" in query["message"][0]
    assert after == before
    assert run is not None
    assert run["source_filename"] == "official & <unsafe>.csv"
    assert run["row_count"] == 5
    assert run["matched_count"] == 1
    assert run["protected_count"] == 1
    assert run["actionable_pending_count"] == 3
    assert run["stats"]["kind_counts"] == {
        "activate_cage": 0,
        "add_cage": 1,
        "add_mice": 1,
        "deactivate_cage": 1,
        "local_ahead": 1,
        "metadata_protected": 0,
        "status_conflict": 0,
    }

    assert review.status_code == 200
    assert "official &amp; &lt;unsafe&gt;.csv · 5 rows" in review.text
    assert "official & <unsafe>.csv" not in review.text
    assert "3 awaiting review" in review.text
    for label, value in (
        ("AOPS rows", 5),
        ("Already matched", 1),
        ("Protected locally", 1),
        ("Applied", 0),
        ("Kept local", 1),
    ):
        assert f"<dt>{label}</dt><dd>{value}</dd>" in review.text
    for group_heading in (
        "New cages",
        "Additional mice",
        "Cages to deactivate",
        "Mouse Line has more records",
    ):
        assert re.search(rf">{re.escape(group_heading)} <span>1</span>", review.text)
    for item in run["items"]:
        assert f'id="aops-item-{item["id"]}"' in review.text
    assert f'action="/aops-reconcile/{reconciliation_id}/apply-all"' in review.text
    assert f'action="/aops-reconcile/{reconciliation_id}/keep-all"' in review.text


@pytest.mark.parametrize(
    ("filename", "payload", "error_text"),
    (
        (
            "malformed.csv",
            b"Cage Card ID,Status\nCC12000001,Active\n",
            "missing required columns",
        ),
        (
            "oversized.csv",
            b"x" * (MAX_AOPS_CSV_BYTES + 1),
            "exceeds",
        ),
    ),
    ids=("malformed", "oversized"),
)
def test_aops_analyze_rejects_invalid_upload_without_creating_a_run(
    tmp_path: Path,
    filename: str,
    payload: bytes,
    error_text: str,
) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        before = _colony_snapshot(database)
        response = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={"csv_file": (filename, payload, "text/csv")},
            follow_redirects=False,
        )
        location = urlsplit(response.headers["location"])
        query = parse_qs(location.query)
        rendered_error = client.get(response.headers["location"])
        run_count = int(
            database.connection.execute("SELECT COUNT(*) FROM aops_reconciliations").fetchone()[0]
        )
        after = _colony_snapshot(database)

    assert response.status_code == 303
    assert location.path == "/"
    assert location.fragment == "aops-review"
    assert "reconciliation" not in query
    assert query["kind"] == ["error"]
    assert error_text in query["message"][0]
    assert rendered_error.status_code == 200
    assert 'class="notice notice--error"' in rendered_error.text
    assert run_count == 0
    assert after == before


def test_aops_analyze_rejects_oversized_request_before_multipart_parsing(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        headers = {
            **_csrf(client),
            "Content-Length": str(AOPS_UPLOAD_REQUEST_MAX_BYTES + 1),
            "Content-Type": "multipart/form-data; boundary=unused",
        }
        response = client.post(
            "/aops-reconcile/analyze",
            headers=headers,
            content=b"",
        )
        run_count = int(
            client.app.state.database.connection.execute(
                "SELECT COUNT(*) FROM aops_reconciliations"
            ).fetchone()[0]
        )

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert run_count == 0


def test_aops_per_item_routes_validate_pair_and_apply_or_keep_selected_changes(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(
            cage_card_id="CC21000001",
            animal_count=1,
            sex="F",
            dob="2026-01-02",
            genotype="local-genotype",
            mouse_user="Local user",
        )
        original_animal = database.list_animals(cage_id)[0]
        original_record = database.get_animal(int(original_animal["id"]))
        assert original_record is not None

        first_analysis = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={
                "csv_file": (
                    "per-item.csv",
                    _aops_csv(
                        ("CC21000001", "Active", "3", "", "", "", ""),
                        ("CC21000002", "Active", "2", "", "", "", ""),
                    ),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
        first_run_id = int(
            parse_qs(urlsplit(first_analysis.headers["location"]).query)["reconciliation"][0]
        )
        first_run = database.get_aops_reconciliation(first_run_id)
        assert first_run is not None
        add_mice_item = first_run["groups"]["add_mice"][0]
        add_cage_item = first_run["groups"]["add_cage"][0]

        second_analysis = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={
                "csv_file": (
                    "other-run.csv",
                    _aops_csv(("CC21999999", "Active", "1", "", "", "", "")),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
        second_run_id = int(
            parse_qs(urlsplit(second_analysis.headers["location"]).query)["reconciliation"][0]
        )
        staged_snapshot = _colony_snapshot(database)
        headers = _csrf(client)

        wrong_run = client.post(
            f"/aops-reconcile/{second_run_id}/items/{add_mice_item['id']}/apply",
            headers=headers,
            follow_redirects=False,
        )
        missing_item = client.post(
            f"/aops-reconcile/{first_run_id}/items/999999/keep-local",
            headers=headers,
            follow_redirects=False,
        )
        missing_review = client.get("/?reconciliation=999999")
        assert _colony_snapshot(database) == staged_snapshot

        applied = client.post(
            f"/aops-reconcile/{first_run_id}/items/{add_mice_item['id']}/apply",
            headers=headers,
            follow_redirects=False,
        )
        kept = client.post(
            f"/aops-reconcile/{first_run_id}/items/{add_cage_item['id']}/keep-local",
            headers=headers,
            follow_redirects=False,
        )
        apply_after_keep = client.post(
            f"/aops-reconcile/{first_run_id}/items/{add_cage_item['id']}/apply",
            headers=headers,
            follow_redirects=False,
        )
        refreshed = database.get_aops_reconciliation(first_run_id)
        aops_movements = database.connection.execute(
            "SELECT movement_type FROM movements WHERE movement_type = 'aops_reconcile'"
        ).fetchall()
        cage = database.get_cage(cage_id)
        original_after = database.get_animal(int(original_animal["id"]))
        cage_count = database.count_cages()

    for invalid_response in (wrong_run, missing_item):
        invalid_query = parse_qs(urlsplit(invalid_response.headers["location"]).query)
        assert invalid_response.status_code == 303
        assert invalid_query["kind"] == ["error"]
        assert invalid_query["message"] == ["AOPS reconciliation item not found."]
    assert missing_review.status_code == 404

    assert applied.status_code == kept.status_code == apply_after_keep.status_code == 303
    assert urlsplit(applied.headers["location"]).fragment == "aops-review"
    assert urlsplit(kept.headers["location"]).fragment == "aops-review"
    assert cage is not None and (cage["active_count"], cage["total_count"]) == (3, 3)
    assert original_after == original_record
    assert cage_count == 1
    assert [row["movement_type"] for row in aops_movements] == [
        "aops_reconcile",
        "aops_reconcile",
    ]
    assert refreshed is not None
    assert refreshed["stats"]["applied"] == 1
    assert refreshed["stats"]["kept_local"] == 1
    assert refreshed["stats"]["pending"] == 0


def test_aops_bulk_routes_apply_all_or_keep_all_pending_differences(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        add_mice_id = database.create_cage(cage_card_id="CC31000001", animal_count=1)
        deactivate_id = database.create_cage(cage_card_id="CC31000002", animal_count=2)

        apply_analysis = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={
                "csv_file": (
                    "bulk-apply.csv",
                    _aops_csv(
                        ("CC31000001", "Active", "3", "", "", "", ""),
                        ("CC31000002", "Deactivated", "2", "", "", "", ""),
                        ("CC31000003", "Active", "2", "", "", "", ""),
                    ),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
        apply_run_id = int(
            parse_qs(urlsplit(apply_analysis.headers["location"]).query)["reconciliation"][0]
        )
        applied = client.post(
            f"/aops-reconcile/{apply_run_id}/apply-all",
            headers=_csrf(client),
            follow_redirects=False,
        )
        applied_run = database.get_aops_reconciliation(apply_run_id)

        keep_cage_id = database.create_cage(cage_card_id="CC31000004", animal_count=1)
        keep_analysis = client.post(
            "/aops-reconcile/analyze",
            headers=_csrf(client),
            files={
                "csv_file": (
                    "bulk-keep.csv",
                    _aops_csv(
                        ("CC31000004", "Active", "3", "", "", "", ""),
                        ("CC31000005", "Active", "1", "", "", "", ""),
                    ),
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
        keep_run_id = int(
            parse_qs(urlsplit(keep_analysis.headers["location"]).query)["reconciliation"][0]
        )
        before_keep = _colony_snapshot(database)
        kept = client.post(
            f"/aops-reconcile/{keep_run_id}/keep-all",
            headers=_csrf(client),
            follow_redirects=False,
        )
        kept_run = database.get_aops_reconciliation(keep_run_id)
        after_keep = _colony_snapshot(database)
        missing_run = client.post(
            "/aops-reconcile/999999/apply-all",
            headers=_csrf(client),
            follow_redirects=False,
        )
        aops_movement_count = int(
            database.connection.execute(
                "SELECT COUNT(*) FROM movements WHERE movement_type = 'aops_reconcile'"
            ).fetchone()[0]
        )
        add_mice_cage = database.get_cage(add_mice_id)
        deactivated_cage = database.get_cage(deactivate_id)
        all_cages = database.list_cages(status=None)
        kept_cage = database.get_cage(keep_cage_id)

    assert applied.status_code == 303
    assert urlsplit(applied.headers["location"]).fragment == "aops-review"
    assert applied_run is not None
    assert applied_run["stats"]["applied"] == 3
    assert applied_run["stats"]["pending"] == 0
    assert add_mice_cage is not None and add_mice_cage["active_count"] == 3
    assert deactivated_cage is not None
    assert (deactivated_cage["status"], deactivated_cage["active_count"]) == (
        "inactive",
        0,
    )
    new_cage = next(cage for cage in all_cages if cage["cage_card_id"] == "CC31000003")
    assert (new_cage["active_count"], new_cage["total_count"]) == (2, 2)
    assert aops_movement_count == 4

    assert kept.status_code == 303
    assert urlsplit(kept.headers["location"]).fragment == "aops-review"
    assert kept_run is not None
    assert kept_run["stats"]["kept_local"] == 2
    assert kept_run["stats"]["pending"] == 0
    assert after_keep == before_keep
    assert kept_cage is not None and kept_cage["active_count"] == 1
    assert all(cage["cage_card_id"] != "CC31000005" for cage in all_cages)

    missing_query = parse_qs(urlsplit(missing_run.headers["location"]).query)
    assert missing_run.status_code == 303
    assert missing_query["kind"] == ["error"]
    assert missing_query["message"] == ["AOPS reconciliation not found."]
