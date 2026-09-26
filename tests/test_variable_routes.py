from __future__ import annotations

import re
from pathlib import Path

from test_routes import _client, _csrf


def _genotype_selects(html: str) -> list[str]:
    return re.findall(r'<select\b[^>]*name="genotype"[^>]*>.*?</select>', html, re.DOTALL)


def test_variable_edits_update_records_and_dropdowns(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        database = client.app.state.database
        cage_id = database.create_cage(
            cage_card_id="VARIABLES", animal_count=2, genotype="Ai32 +/-"
        )
        headers = _csrf(client)
        added = client.post("/variables/genotype/add", data={"name": "Ai32 +/-"}, headers=headers)
        assert added.status_code == 200
        assert "Added." in added.text
        option_id = database.list_variable_options("genotype")[0]["id"]

        renamed = client.post(
            f"/variables/{option_id}/rename", data={"name": "Ai32 heterozygous"}, headers=headers
        )
        assert "Saved." in renamed.text
        assert all(
            animal["genotype"] == "Ai32 heterozygous" for animal in database.list_animals(cage_id)
        )
        cage = client.get(f"/cages/{cage_id}")
        assert 'name="genotype"' in cage.text
        assert 'value="Ai32 heterozygous" selected' in cage.text
        assert "data-save-all-mice" in cage.text

        deleted = client.post(f"/variables/{option_id}/delete", headers=headers)
        assert "Deleted." in deleted.text
        assert database.list_variable_options("genotype") == []
        assert "Ai32 heterozygous" not in "".join(_genotype_selects(client.get("/").text))
        assert 'value="Ai32 heterozygous" selected' in client.get(f"/cages/{cage_id}").text

        assert client.post(f"/variables/{option_id}/toggle", headers=headers).status_code == 404
        missing = client.post(f"/variables/{option_id}/delete", headers=headers)
        assert "Variable option not found." in missing.text


def test_variables_page_respects_mount_and_request_security(tmp_path: Path) -> None:
    with _client(tmp_path, root_path="/colony") as client:
        page = client.get("/variables")
        assert page.status_code == 200
        for label in ("Genotypes", "Mouse users", "Surgery types", "Surgery operators", "Rooms"):
            assert label in page.text
        assert 'action="/colony/variables/genotype/add"' in page.text
        assert 'href="/colony/variables"' in client.get("/").text
        assert client.post("/variables/genotype/add", data={"name": "WT"}).status_code == 403
        option_id = client.app.state.database.list_variable_options("surgery_type")[0]["id"]
        assert client.post(f"/variables/{option_id}/delete").status_code == 403

    with _client(tmp_path / "anonymous", authenticated=False) as client:
        assert client.get("/variables", follow_redirects=False).status_code == 303
        assert client.app.state.database.list_variable_options("genotype") == []


def test_variable_errors_and_names_are_rendered_safely(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        headers = _csrf(client)
        page = client.post("/variables/genotype/add", data={"name": "<example>"}, headers=headers)
        assert page.status_code == 200
        assert "&lt;example&gt;" in page.text
        duplicate = client.post(
            "/variables/genotype/add", data={"name": "<example>"}, headers=headers
        )
        assert "notice--error" in duplicate.text
        invalid = client.post("/variables/unknown/add", data={"name": "test"}, headers=headers)
        assert "notice--error" in invalid.text
