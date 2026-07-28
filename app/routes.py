"""HTML routes for the shared local mouseline database."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import Database
from app.security import csrf_token_for_request

router = APIRouter()

_BATCH_ANIMAL_FIELD_LABELS = {
    "sex": "sex",
    "genotype": "genotype",
    "dob": "date of birth",
    "mouse_user": "mouse user",
}
_CAGE_FILTER_STATUSES = {"all", "active", "inactive", "on_order"}
_CAGE_SORT_FIELDS = {"cage_card_id", "room", "status"}
_SORT_DIRECTIONS = {"asc", "desc"}


def _db(request: Request) -> Database:
    return request.app.state.database


def _base_path(request: Request) -> str:
    return str(request.scope.get("root_path", "")).rstrip("/")


def _app_path(request: Request, path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{_base_path(request)}{normalized_path}"


def _redirect(
    request: Request,
    path: str,
    message: str,
    *,
    kind: str = "success",
) -> RedirectResponse:
    path_without_fragment, hash_marker, fragment = path.partition("#")
    separator = "&" if "?" in path_without_fragment else "?"
    query = urlencode({"message": message, "kind": kind})
    location = f"{_app_path(request, path_without_fragment)}{separator}{query}"
    if hash_marker:
        location = f"{location}#{fragment}"
    return RedirectResponse(
        location,
        status_code=303,
    )


def _clean(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None


def _sex(value: str) -> str:
    return {
        "m": "M",
        "male": "M",
        "f": "F",
        "female": "F",
        "u": "U",
        "unknown": "U",
    }.get(value.strip().casefold(), "U")


def _canonical_choice(value: str, choices: set[str], default: str) -> str:
    candidate = value.strip().casefold()
    return candidate if candidate in choices else default


def _cage_view_urls(
    request: Request,
    *,
    search: str,
    status: str,
    tag: str,
    mouse_user: str,
    room: str,
    sort: str,
    direction: str,
) -> dict[str, str]:
    shared: dict[str, str] = {}
    if cleaned_search := _clean(search):
        shared["search"] = cleaned_search
    if status != "all":
        shared["status"] = status
    if cleaned_tag := _clean(tag):
        shared["tag"] = cleaned_tag
    if cleaned_mouse_user := _clean(mouse_user):
        shared["mouse_user"] = cleaned_mouse_user
    if cleaned_room := _clean(room):
        shared["room"] = cleaned_room
    if sort != "status":
        shared["sort"] = sort
    if direction != "asc":
        shared["direction"] = direction
    return {
        view_name: _app_path(
            request,
            f"/?{urlencode({'view': view_name, **shared})}#cages",
        )
        for view_name in ("all", "stock", "using", "breeding")
    }


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    search: str = "",
    status: str = "all",
    tag: str = "",
    mouse_user: str = "",
    room: str = "",
    sort: str = "status",
    direction: str = "asc",
    view: Literal["all", "stock", "using", "single", "breeding"] = "all",
    message: str = "",
    kind: str = "success",
) -> HTMLResponse:
    database = _db(request)
    canonical_view = "using" if view == "single" else view
    canonical_status = _canonical_choice(status, _CAGE_FILTER_STATUSES, "all")
    canonical_sort = _canonical_choice(sort, _CAGE_SORT_FIELDS, "status")
    canonical_direction = _canonical_choice(direction, _SORT_DIRECTIONS, "asc")
    cleaned_mouse_user = _clean(mouse_user)
    cleaned_room = _clean(room)
    cages = database.list_cages(
        search=_clean(search),
        status=None if canonical_status == "all" else canonical_status,
        tag=_clean(tag),
        mouse_user=cleaned_mouse_user,
        room=cleaned_room,
        sort=canonical_sort,
        direction=canonical_direction,
        view=canonical_view,
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "base_path": _base_path(request),
            "csrf_token": csrf_token_for_request(request),
            "summary": database.summary(),
            "cages": cages,
            "all_tags": database.list_tags(),
            "mouse_users": database.list_mouse_users(),
            "rooms": database.list_rooms(),
            "filters": {
                "search": search,
                "status": canonical_status,
                "tag": tag,
                "mouse_user": cleaned_mouse_user or "",
                "room": cleaned_room or "",
                "sort": canonical_sort,
                "direction": canonical_direction,
                "view": canonical_view,
            },
            "view_urls": _cage_view_urls(
                request,
                search=search,
                status=canonical_status,
                tag=tag,
                mouse_user=cleaned_mouse_user or "",
                room=cleaned_room or "",
                sort=canonical_sort,
                direction=canonical_direction,
            ),
            "message": message,
            "message_kind": kind,
            "seed_report": request.app.state.seed_report,
        },
    )


@router.get("/cages/{cage_id}", response_class=HTMLResponse)
def cage_detail(
    cage_id: int,
    request: Request,
    message: str = "",
    kind: str = "success",
) -> HTMLResponse:
    database = _db(request)
    cage = database.get_cage(cage_id)
    if cage is None:
        raise HTTPException(status_code=404, detail="Cage not found.")
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="cage.html",
        context={
            "base_path": _base_path(request),
            "csrf_token": csrf_token_for_request(request),
            "cage": cage,
            "animals": database.list_animals(cage_id, include_inactive=True),
            "all_tags": database.list_tags(),
            "mouse_users": database.list_mouse_users(),
            "operators": database.list_operators(),
            "surgery_types": database.list_surgery_types(),
            "message": message,
            "message_kind": kind,
        },
    )


@router.get("/healthz")
def healthz(request: Request) -> dict[str, Any]:
    return {"status": "ok", **_db(request).summary()}


@router.post("/cages/new")
def create_cage(
    request: Request,
    cage_card_id: Annotated[str, Form()] = "",
    count: Annotated[int, Form(ge=0, le=100)] = 0,
    sex: Annotated[str, Form()] = "U",
    dob: Annotated[str, Form()] = "",
    genotype: Annotated[str, Form()] = "",
    mouse_user: Annotated[str, Form()] = "",
    room: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    is_breeding_pair: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    try:
        cage_id = _db(request).create_cage(
            cage_card_id=_clean(cage_card_id),
            animal_count=count,
            sex=_sex(sex),
            dob=_clean(dob),
            genotype=_clean(genotype),
            mouse_user=_clean(mouse_user),
            room=_clean(room),
            note=_clean(note),
            creation_type="manual",
            is_breeding_pair=is_breeding_pair,
        )
    except ValueError as exc:
        return _redirect(request, "/", str(exc), kind="error")
    return _redirect(
        request,
        f"/cages/{cage_id}",
        "Cage created and mouse IDs assigned.",
    )


@router.post("/cages/{cage_id}/add-mice")
def add_mice(
    cage_id: int,
    request: Request,
    count: Annotated[int, Form(ge=1, le=100)],
    sex: Annotated[str, Form()] = "U",
    dob: Annotated[str, Form()] = "",
    genotype: Annotated[str, Form()] = "",
    mouse_user: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        created = _db(request).add_animals(
            cage_id,
            count=count,
            sex=_sex(sex),
            dob=_clean(dob),
            genotype=_clean(genotype),
            mouse_user=_clean(mouse_user),
            note=_clean(note),
        )
    except ValueError as exc:
        return _redirect(request, f"/cages/{cage_id}", str(exc), kind="error")
    return _redirect(request, f"/cages/{cage_id}", f"Added {len(created)} mice.")


@router.post("/cages/{cage_id}/split")
def split_cage(
    cage_id: int,
    request: Request,
    animal_ids: Annotated[list[int] | None, Form()] = None,
    destination_cage_card_id: Annotated[str, Form()] = "",
    destination_room: Annotated[str, Form()] = "",
    destination_note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if not animal_ids:
        return _redirect(
            request,
            f"/cages/{cage_id}",
            "Select at least one mouse.",
            kind="error",
        )
    try:
        destination_id = _db(request).split_cage(
            cage_id,
            animal_ids=animal_ids,
            cage_card_id=_clean(destination_cage_card_id),
            room=_clean(destination_room),
            note=_clean(destination_note),
        )
    except ValueError as exc:
        return _redirect(request, f"/cages/{cage_id}", str(exc), kind="error")
    return _redirect(
        request,
        f"/cages/{destination_id}",
        f"Moved {len(animal_ids)} mice into the new cage.",
    )


@router.post("/cages/{cage_id}/wean")
def wean_cage(
    cage_id: int,
    request: Request,
    count: Annotated[int, Form(ge=1, le=100)],
    sex: Annotated[str, Form()] = "U",
    dob: Annotated[str, Form()] = "",
    genotype: Annotated[str, Form()] = "",
    mouse_user: Annotated[str, Form()] = "",
    destination_cage_card_id: Annotated[str, Form()] = "",
    destination_room: Annotated[str, Form()] = "",
    destination_note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        destination_id = _db(request).wean_cage(
            cage_id,
            count=count,
            sex=_sex(sex),
            dob=_clean(dob),
            genotype=_clean(genotype),
            mouse_user=_clean(mouse_user),
            cage_card_id=_clean(destination_cage_card_id),
            room=_clean(destination_room),
            note=_clean(destination_note),
        )
    except ValueError as exc:
        return _redirect(request, f"/cages/{cage_id}", str(exc), kind="error")
    return _redirect(
        request,
        f"/cages/{destination_id}",
        f"Created a wean cage with {count} mice.",
    )


@router.post("/cages/{cage_id}/toggle")
def toggle_cage(cage_id: int, request: Request) -> RedirectResponse:
    try:
        cage = _db(request).toggle_cage(cage_id)
    except ValueError as exc:
        return _redirect(request, f"/cages/{cage_id}", str(exc), kind="error")
    return _redirect(request, f"/cages/{cage_id}", f"Cage marked {cage['status']}.")


@router.post("/cages/{cage_id}/update")
def update_cage(
    cage_id: int,
    request: Request,
    cage_card_id: Annotated[str, Form()] = "",
    room: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    on_census_date: Annotated[str, Form()] = "",
    off_census_date: Annotated[str, Form()] = "",
    is_breeding_pair: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    try:
        _db(request).update_cage(
            cage_id,
            cage_card_id=cage_card_id,
            room=_clean(room),
            note=_clean(note),
            on_census_date=_clean(on_census_date),
            off_census_date=_clean(off_census_date),
            is_breeding_pair=is_breeding_pair,
        )
    except ValueError as exc:
        return _redirect(request, f"/cages/{cage_id}", str(exc), kind="error")
    return _redirect(request, f"/cages/{cage_id}", "Cage details updated.")


@router.post("/cages/{cage_id}/animals/batch-update")
def batch_update_cage_animals(
    cage_id: int,
    request: Request,
    field: Annotated[str, Form()],
    value: Annotated[str, Form()] = "",
) -> RedirectResponse:
    normalized_field = field.strip().casefold()
    field_label = _BATCH_ANIMAL_FIELD_LABELS.get(normalized_field)
    if field_label is None:
        return _redirect(
            request,
            f"/cages/{cage_id}#mice",
            "Choose sex, genotype, date of birth, or mouse user.",
            kind="error",
        )

    cleaned_value = _clean(value)
    try:
        updated_count = _db(request).batch_update_animals(
            cage_id,
            field=normalized_field,
            value=cleaned_value,
        )
    except ValueError as exc:
        return _redirect(
            request,
            f"/cages/{cage_id}#mice",
            str(exc),
            kind="error",
        )

    mouse_label = "mouse" if updated_count == 1 else "mice"
    action = "Cleared" if cleaned_value is None else "Updated"
    return _redirect(
        request,
        f"/cages/{cage_id}#mice",
        f"{action} {field_label} for {updated_count} {mouse_label}.",
    )


@router.post("/cages/{cage_id}/tags")
def add_cage_tag(
    cage_id: int,
    request: Request,
    tag_name: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        _db(request).add_tag(cage_id, tag_name)
    except ValueError as exc:
        return _redirect(request, f"/cages/{cage_id}", str(exc), kind="error")
    return _redirect(request, f"/cages/{cage_id}", "Tag added.")


@router.post("/cages/{cage_id}/tags/{tag_id}/remove")
def remove_cage_tag(cage_id: int, tag_id: int, request: Request) -> RedirectResponse:
    _db(request).remove_tag(cage_id, tag_id)
    return _redirect(request, f"/cages/{cage_id}", "Tag removed.")


@router.post("/animals/{animal_id}/update")
def update_animal(
    animal_id: int,
    request: Request,
    legacy_id: Annotated[str, Form()] = "",
    sex: Annotated[str, Form()] = "U",
    dob: Annotated[str, Form()] = "",
    genotype: Annotated[str, Form()] = "",
    mouse_user: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> RedirectResponse:
    database = _db(request)
    animal = database.get_animal(animal_id)
    if animal is None:
        raise HTTPException(status_code=404, detail="Mouse not found.")
    try:
        database.update_animal(
            animal_id,
            legacy_id=_clean(legacy_id),
            sex=_sex(sex),
            dob=_clean(dob),
            genotype=_clean(genotype),
            mouse_user=_clean(mouse_user),
            note=_clean(note),
        )
    except ValueError as exc:
        return _redirect(
            request,
            f"/cages/{animal['cage_id']}",
            str(exc),
            kind="error",
        )
    return _redirect(
        request,
        f"/cages/{animal['cage_id']}",
        "Mouse details updated.",
    )


@router.post("/animals/{animal_id}/toggle")
def toggle_animal(animal_id: int, request: Request) -> RedirectResponse:
    database = _db(request)
    animal = database.get_animal(animal_id)
    if animal is None:
        raise HTTPException(status_code=404, detail="Mouse not found.")
    updated = database.toggle_animal(animal_id)
    return _redirect(
        request,
        f"/cages/{animal['cage_id']}",
        f"{animal['public_id']} marked {updated['status']}.",
    )


@router.post("/animals/{animal_id}/surgery")
def add_surgery(
    animal_id: int,
    request: Request,
    surgery_date: Annotated[str, Form()],
    surgery_time: Annotated[str, Form()] = "",
    operator: Annotated[str, Form()] = "",
    surgery_type: Annotated[str, Form()] = "",
) -> RedirectResponse:
    database = _db(request)
    animal = database.get_animal(animal_id)
    if animal is None:
        raise HTTPException(status_code=404, detail="Mouse not found.")
    try:
        database.add_surgery(
            animal_id,
            surgery_date=surgery_date,
            surgery_time=_clean(surgery_time),
            operator=operator,
            surgery_type=surgery_type,
        )
    except ValueError as exc:
        return _redirect(
            request,
            f"/cages/{animal['cage_id']}",
            str(exc),
            kind="error",
        )
    return _redirect(
        request,
        f"/cages/{animal['cage_id']}",
        "Surgery recorded.",
    )


@router.post("/surgeries/{surgery_id}/update")
def update_surgery(
    surgery_id: int,
    request: Request,
    surgery_date: Annotated[str, Form()],
    surgery_time: Annotated[str, Form()] = "",
    operator: Annotated[str, Form()] = "",
    surgery_type: Annotated[str, Form()] = "",
) -> RedirectResponse:
    database = _db(request)
    surgery = database.get_surgery(surgery_id)
    if surgery is None:
        raise HTTPException(status_code=404, detail="Surgery record not found.")
    animal = database.get_animal(int(surgery["animal_id"]))
    if animal is None or int(animal["cage_id"]) != int(surgery["cage_id"]):
        raise HTTPException(status_code=404, detail="Mouse not found.")
    cage_id = int(animal["cage_id"])
    if database.get_cage(cage_id) is None:
        raise HTTPException(status_code=404, detail="Cage not found.")
    try:
        database.update_surgery(
            surgery_id,
            surgery_date=surgery_date,
            surgery_time=_clean(surgery_time),
            operator=operator,
            surgery_type=surgery_type,
        )
    except ValueError as exc:
        return _redirect(
            request,
            f"/cages/{cage_id}",
            str(exc),
            kind="error",
        )
    return _redirect(
        request,
        f"/cages/{cage_id}",
        "Surgery details updated.",
    )
