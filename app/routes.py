"""HTML routes for the shared local mouseline database."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import Database
from app.photo_import import photos_for_cage
from app.reconciliation import (
    MAX_AOPS_CSV_BYTES,
    AopsCsvValidationError,
    parse_aops_cage_cards,
)
from app.security import (
    LOGIN_COOKIE_NAME,
    LOGIN_SESSION_MAX_AGE_SECONDS,
    LoginManager,
    csrf_token_for_request,
)

router = APIRouter()

_BATCH_ANIMAL_FIELD_LABELS = {
    "sex": "sex",
    "genotype": "genotype",
    "dob": "date of birth",
    "mouse_user": "mouse user",
}
_VARIABLE_GROUP_LABELS = {
    "genotype": "Genotypes",
    "mouse_user": "Mouse users",
    "surgery_type": "Surgery types",
    "operator": "Surgery operators",
    "room": "Rooms",
}
_CAGE_FILTER_STATUSES = {"all", "active", "inactive", "on_order"}
_CAGE_SORT_FIELDS = {"cage_card_id", "room", "status"}
_SORT_DIRECTIONS = {"asc", "desc"}
_CAGE_VIEWS = {"all", "stock", "using", "breeding"}
_CAGE_RETURN_QUERY_KEYS = {
    "view",
    "search",
    "status",
    "tag",
    "mouse_user",
    "room",
    "sort",
    "direction",
}


def _db(request: Request) -> Database:
    return request.app.state.database


def _variable_form_options(database: Database) -> dict[str, list[str]]:
    return {
        f"{category}_options": [
            option["name"] for option in database.list_variable_options(category)
        ]
        for category in _VARIABLE_GROUP_LABELS
    }


def _login_manager(request: Request) -> LoginManager:
    return request.app.state.login_manager


def _base_path(request: Request) -> str:
    return str(request.scope.get("root_path", "")).rstrip("/")


def _app_path(request: Request, path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{_base_path(request)}{normalized_path}"


def _login_return_url(request: Request, value: str) -> str:
    base_path = _base_path(request)
    fallback = f"{base_path}/"
    candidate = value.strip()
    if not candidate or len(candidate) > 2048:
        return fallback
    if "\\" in candidate or any(ord(character) < 32 for character in candidate):
        return fallback
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return fallback
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return fallback
    decoded_path = unquote(parsed.path)
    if (
        decoded_path.startswith("//")
        or "\\" in decoded_path
        or any(ord(character) < 32 for character in decoded_path)
    ):
        return fallback
    required_prefix = f"{base_path}/" if base_path else "/"
    if not parsed.path.startswith(required_prefix):
        return fallback
    route_path = parsed.path[len(base_path) :] if base_path else parsed.path
    if route_path in {"/login", "/logout"} or route_path.startswith("/static/"):
        return fallback
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.path}{query}"


def _cage_return_url(request: Request, value: str) -> str:
    fallback = _app_path(request, "/#cages")
    candidate = value.strip()
    if not candidate or len(candidate) > 2048:
        return fallback
    if "\\" in candidate or any(ord(character) < 32 for character in candidate):
        return fallback

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return fallback
    if parsed.scheme or parsed.netloc or parsed.path != _app_path(request, "/"):
        return fallback
    if parsed.fragment not in {"", "cages"}:
        return fallback

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_keys = [key for key, _value in query_pairs]
    if len(query_keys) != len(set(query_keys)):
        return fallback
    if any(key not in _CAGE_RETURN_QUERY_KEYS for key in query_keys):
        return fallback
    if any(
        "\\" in item or any(ord(character) < 32 for character in item)
        for pair in query_pairs
        for item in pair
    ):
        return fallback

    query_values = dict(query_pairs)
    if query_values.get("view", "all") not in _CAGE_VIEWS:
        return fallback
    if query_values.get("status", "active") not in _CAGE_FILTER_STATUSES:
        return fallback
    if query_values.get("sort", "status") not in _CAGE_SORT_FIELDS:
        return fallback
    if query_values.get("direction", "asc") not in _SORT_DIRECTIONS:
        return fallback

    query = f"?{urlencode(query_pairs)}" if query_pairs else ""
    return f"{parsed.path}{query}#cages"


def _redirect(
    request: Request,
    path: str,
    message: str,
    *,
    kind: str = "success",
) -> RedirectResponse:
    path_without_fragment, hash_marker, fragment = path.partition("#")
    separator = "&" if "?" in path_without_fragment else "?"
    query_values = {"message": message, "kind": kind}
    if "return_to" in request.query_params:
        query_values["return_to"] = _cage_return_url(
            request,
            request.query_params.get("return_to", ""),
        )
    query = urlencode(query_values)
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


def _aops_review_path(reconciliation_id: int | None = None) -> str:
    query = f"?{urlencode({'reconciliation': reconciliation_id})}" if reconciliation_id else ""
    return f"/{query}#aops-review"


def _safe_upload_filename(value: str | None) -> str:
    candidate = (value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    printable = "".join(character for character in candidate if character.isprintable())
    return printable[:255] or "AOPS Cage Cards.csv"


def _cage_list_url(
    request: Request,
    *,
    view: str,
    search: str,
    status: str,
    tag: str,
    mouse_user: str,
    room: str,
    sort: str,
    direction: str,
    sort_explicit: bool,
) -> str:
    query: dict[str, str] = {"view": view}
    if cleaned_search := _clean(search):
        query["search"] = cleaned_search
    if status != "active":
        query["status"] = status
    if cleaned_tag := _clean(tag):
        query["tag"] = cleaned_tag
    if cleaned_mouse_user := _clean(mouse_user):
        query["mouse_user"] = cleaned_mouse_user
    if cleaned_room := _clean(room):
        query["room"] = cleaned_room
    if sort_explicit:
        query["sort"] = sort
        query["direction"] = direction
    return _app_path(request, f"/?{urlencode(query)}#cages")


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
    sort_explicit: bool,
) -> dict[str, str]:
    return {
        view_name: _cage_list_url(
            request,
            view=view_name,
            search=search,
            status=status,
            tag=tag,
            mouse_user=mouse_user,
            room=room,
            sort=sort,
            direction=direction,
            sort_explicit=sort_explicit,
        )
        for view_name in ("all", "stock", "using", "breeding")
    }


def _cage_sort_urls(
    request: Request,
    *,
    view: str,
    search: str,
    status: str,
    tag: str,
    mouse_user: str,
    room: str,
    sort: str,
    direction: str,
    sort_explicit: bool,
) -> dict[str, str]:
    urls: dict[str, str] = {}
    for field in ("cage_card_id", "room", "status"):
        next_sort = field
        next_direction = "asc"
        next_explicit = True
        if sort_explicit and sort == field:
            if direction == "asc":
                next_direction = "desc"
            else:
                next_sort = "status"
                next_explicit = False
        urls[field] = _cage_list_url(
            request,
            view=view,
            search=search,
            status=status,
            tag=tag,
            mouse_user=mouse_user,
            room=room,
            sort=next_sort,
            direction=next_direction,
            sort_explicit=next_explicit,
        )
    return urls


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "") -> Response:
    return_to = _login_return_url(request, next)
    if _login_manager(request).validate_session(request.cookies.get(LOGIN_COOKIE_NAME)):
        return RedirectResponse(return_to, status_code=303)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "base_path": _base_path(request),
            "return_to": return_to,
            "error": False,
        },
    )


@router.post("/login", response_class=HTMLResponse)
def submit_login(
    request: Request,
    answer: Annotated[str, Form(max_length=100)] = "",
    next: Annotated[str, Form(max_length=2048)] = "",
) -> Response:
    return_to = _login_return_url(request, next)
    login_manager = _login_manager(request)
    if not login_manager.validate_answer(answer):
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "base_path": _base_path(request),
                "return_to": return_to,
                "error": True,
            },
            status_code=401,
        )

    response = RedirectResponse(return_to, status_code=303)
    response.set_cookie(
        LOGIN_COOKIE_NAME,
        login_manager.issue_session_token(),
        max_age=LOGIN_SESSION_MAX_AGE_SECONDS,
        path=_base_path(request) or "/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    _login_manager(request).revoke_session(request.cookies.get(LOGIN_COOKIE_NAME))
    response = RedirectResponse(_app_path(request, "/login"), status_code=303)
    response.delete_cookie(
        LOGIN_COOKIE_NAME,
        path=_base_path(request) or "/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    search: str = "",
    status: str = "active",
    tag: str = "",
    mouse_user: str = "",
    room: str = "",
    sort: str = "status",
    direction: str = "asc",
    view: Literal["all", "stock", "using", "single", "breeding"] = "all",
    reconciliation: int | None = None,
    message: str = "",
    kind: str = "success",
) -> HTMLResponse:
    database = _db(request)
    aops_reconciliation = None
    if reconciliation is not None:
        aops_reconciliation = database.get_aops_reconciliation(reconciliation)
        if aops_reconciliation is None:
            raise HTTPException(status_code=404, detail="AOPS review not found.")
    elif kind != "error":
        aops_reconciliation = database.get_latest_aops_reconciliation()
    canonical_view = "using" if view == "single" else view
    canonical_status = _canonical_choice(status, _CAGE_FILTER_STATUSES, "active")
    sort_candidate = sort.strip().casefold()
    sort_explicit = "sort" in request.query_params and sort_candidate in _CAGE_SORT_FIELDS
    canonical_sort = sort_candidate if sort_explicit else "status"
    canonical_direction = (
        _canonical_choice(direction, _SORT_DIRECTIONS, "asc") if sort_explicit else "asc"
    )
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
    view_urls = _cage_view_urls(
        request,
        search=search,
        status=canonical_status,
        tag=tag,
        mouse_user=cleaned_mouse_user or "",
        room=cleaned_room or "",
        sort=canonical_sort,
        direction=canonical_direction,
        sort_explicit=sort_explicit,
    )
    sort_urls = _cage_sort_urls(
        request,
        view=canonical_view,
        search=search,
        status=canonical_status,
        tag=tag,
        mouse_user=cleaned_mouse_user or "",
        room=cleaned_room or "",
        sort=canonical_sort,
        direction=canonical_direction,
        sort_explicit=sort_explicit,
    )
    cage_return_to = view_urls[canonical_view]
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
            **_variable_form_options(database),
            "filters": {
                "search": search,
                "status": canonical_status,
                "tag": tag,
                "mouse_user": cleaned_mouse_user or "",
                "room": cleaned_room or "",
                "sort": canonical_sort,
                "direction": canonical_direction,
                "sort_explicit": sort_explicit,
                "view": canonical_view,
            },
            "view_urls": view_urls,
            "sort_urls": sort_urls,
            "cage_detail_urls": {
                cage["id"]: _app_path(
                    request,
                    f"/cages/{cage['id']}?{urlencode({'return_to': cage_return_to})}",
                )
                for cage in cages
            },
            "message": message,
            "message_kind": kind,
            "seed_report": request.app.state.seed_report,
            "aops_reconciliation": aops_reconciliation,
        },
    )


@router.get("/cages/{cage_id}", response_class=HTMLResponse)
def cage_detail(
    cage_id: int,
    request: Request,
    return_to: str = "",
    message: str = "",
    kind: str = "success",
) -> HTMLResponse:
    database = _db(request)
    cage = database.get_cage(cage_id)
    if cage is None:
        raise HTTPException(status_code=404, detail="Cage not found.")
    back_url = _cage_return_url(request, return_to)
    today = date.today()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="cage.html",
        context={
            "base_path": _base_path(request),
            "back_url": back_url,
            "return_query": f"?{urlencode({'return_to': back_url})}",
            "csrf_token": csrf_token_for_request(request),
            "cage": cage,
            "photos": photos_for_cage(request, cage_id),
            "animals": database.list_animals(cage_id, include_inactive=True),
            **_variable_form_options(database),
            "all_tags": database.list_tags(),
            "mouse_users": database.list_mouse_users(),
            "today": today.isoformat(),
            "default_wean_dob": (today - timedelta(days=21)).isoformat(),
            "message": message,
            "message_kind": kind,
        },
    )


@router.get("/variables", response_class=HTMLResponse)
def variables_page(
    request: Request,
    message: str = "",
    kind: str = "success",
) -> HTMLResponse:
    database = _db(request)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="variables.html",
        context={
            "base_path": _base_path(request),
            "csrf_token": csrf_token_for_request(request),
            "variable_groups": [
                {
                    "key": category,
                    "label": label,
                    "items": database.list_variable_options(category),
                }
                for category, label in _VARIABLE_GROUP_LABELS.items()
            ],
            "message": message,
            "message_kind": kind,
            "show_local_logout": True,
        },
    )


@router.post("/variables/{category}/add")
def add_variable_option(
    category: str,
    request: Request,
    name: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        _db(request).add_variable_option(category, name)
    except ValueError as exc:
        return _redirect(request, "/variables", str(exc), kind="error")
    return _redirect(request, "/variables", "Added.")


@router.post("/variables/{option_id}/rename")
def rename_variable_option(
    option_id: int,
    request: Request,
    name: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        _db(request).rename_variable_option(option_id, name)
    except ValueError as exc:
        return _redirect(request, "/variables", str(exc), kind="error")
    return _redirect(request, "/variables", "Saved.")


@router.post("/variables/{option_id}/delete")
def delete_variable_option(
    option_id: int,
    request: Request,
) -> RedirectResponse:
    try:
        _db(request).delete_variable_option(option_id)
    except ValueError as exc:
        return _redirect(request, "/variables", str(exc), kind="error")
    return _redirect(request, "/variables", "Deleted.")


@router.get("/healthz")
def healthz(request: Request) -> dict[str, Any]:
    return {"status": "ok", **_db(request).summary()}


@router.post("/aops-reconcile/analyze")
async def analyze_aops_reconciliation(
    request: Request,
    csv_file: Annotated[UploadFile, File()],
) -> RedirectResponse:
    try:
        payload = await csv_file.read(MAX_AOPS_CSV_BYTES + 1)
        parsed = parse_aops_cage_cards(payload)
        rows = [
            {
                "cage_card_id": record.cage_card_id,
                "status": record.status,
                "count": record.animal_count,
                "room": record.room,
                "protocol": record.protocol,
                "on_census_date": record.on_census_date,
                "off_census_date": record.off_census_date,
            }
            for record in parsed.records
        ]
        reconciliation = _db(request).create_aops_reconciliation(
            source_filename=_safe_upload_filename(csv_file.filename),
            source_sha256=hashlib.sha256(payload).hexdigest(),
            rows=rows,
        )
    except AopsCsvValidationError as exc:
        return _redirect(request, _aops_review_path(), str(exc), kind="error")
    except ValueError as exc:
        return _redirect(request, _aops_review_path(), str(exc), kind="error")
    except (OSError, UnicodeError):
        return _redirect(
            request,
            _aops_review_path(),
            "The uploaded CSV could not be read.",
            kind="error",
        )
    finally:
        await csv_file.close()

    pending_count = int(reconciliation.get("actionable_pending_count", 0))
    matched_count = int(reconciliation.get("matched_count", 0))
    message = (
        f"Analysis complete: {pending_count} update"
        f"{'s' if pending_count != 1 else ''} to review and {matched_count} AOPS row"
        f"{'s' if matched_count != 1 else ''} already matched. No colony data changed."
    )
    return _redirect(
        request,
        _aops_review_path(int(reconciliation["id"])),
        message,
    )


@router.post("/aops-reconcile/{reconciliation_id}/items/{item_id}/apply")
def apply_aops_reconciliation_item(
    reconciliation_id: int,
    item_id: int,
    request: Request,
) -> RedirectResponse:
    try:
        item = _db(request).apply_aops_reconciliation_item(reconciliation_id, item_id)
    except ValueError as exc:
        return _redirect(
            request,
            _aops_review_path(reconciliation_id),
            str(exc),
            kind="error",
        )
    message = str(item.get("resolution_note") or "AOPS update applied.")
    return _redirect(request, _aops_review_path(reconciliation_id), message)


@router.post("/aops-reconcile/{reconciliation_id}/items/{item_id}/keep-local")
def keep_local_aops_reconciliation_item(
    reconciliation_id: int,
    item_id: int,
    request: Request,
) -> RedirectResponse:
    try:
        _db(request).keep_local_aops_reconciliation_item(reconciliation_id, item_id)
    except ValueError as exc:
        return _redirect(
            request,
            _aops_review_path(reconciliation_id),
            str(exc),
            kind="error",
        )
    return _redirect(
        request,
        _aops_review_path(reconciliation_id),
        "Kept the Mouse Line record. No colony data changed.",
    )


@router.post("/aops-reconcile/{reconciliation_id}/apply-all")
def apply_all_aops_reconciliation(
    reconciliation_id: int,
    request: Request,
) -> RedirectResponse:
    try:
        review = _db(request).apply_all_aops_reconciliation(reconciliation_id)
    except ValueError as exc:
        return _redirect(
            request,
            _aops_review_path(reconciliation_id),
            str(exc),
            kind="error",
        )
    applied_count = int(review["applied_count"])
    kept_count = int(review["kept_local_count"])
    return _redirect(
        request,
        _aops_review_path(reconciliation_id),
        f"CSV update complete: {applied_count} update{'s' if applied_count != 1 else ''} applied; "
        f"{kept_count} difference{'s' if kept_count != 1 else ''} kept local. "
        "Existing mouse details and history were kept.",
    )


@router.post("/aops-reconcile/{reconciliation_id}/keep-all")
def keep_local_all_aops_reconciliation(
    reconciliation_id: int,
    request: Request,
) -> RedirectResponse:
    try:
        _db(request).keep_local_all_aops_reconciliation(reconciliation_id)
    except ValueError as exc:
        return _redirect(
            request,
            _aops_review_path(reconciliation_id),
            str(exc),
            kind="error",
        )
    return _redirect(
        request,
        _aops_review_path(reconciliation_id),
        "Kept all pending differences in Mouse Line. No colony data changed.",
    )


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
            dob=_clean(dob) or (date.today() - timedelta(days=21)).isoformat(),
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
    field: Annotated[str | None, Form()] = None,
    value: Annotated[str | None, Form()] = None,
    batch_mode: Annotated[str, Form()] = "",
    sex: Annotated[str | None, Form()] = None,
    genotype: Annotated[str | None, Form()] = None,
    dob: Annotated[str | None, Form()] = None,
    mouse_user: Annotated[str | None, Form()] = None,
    change_sex: Annotated[bool, Form()] = False,
    change_genotype: Annotated[bool, Form()] = False,
    change_dob: Annotated[bool, Form()] = False,
    change_mouse_user: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    if field is not None:
        normalized_field = field.strip().casefold()
        field_label = _BATCH_ANIMAL_FIELD_LABELS.get(normalized_field)
        if field_label is None:
            return _redirect(
                request,
                f"/cages/{cage_id}#mice",
                "Choose sex, genotype, date of birth, or mouse user.",
                kind="error",
            )

        cleaned_value = _clean(value or "")
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

    submitted_properties = {
        "sex": _clean(sex or ""),
        "genotype": _clean(genotype or ""),
        "dob": _clean(dob or ""),
        "mouse_user": _clean(mouse_user or ""),
    }
    if batch_mode == "selected":
        selected = {
            name: submitted_properties[name]
            for name, should_change in (
                ("sex", change_sex),
                ("genotype", change_genotype),
                ("dob", change_dob),
                ("mouse_user", change_mouse_user),
            )
            if should_change
        }
    else:
        provided = {
            "sex": sex,
            "genotype": genotype,
            "dob": dob,
            "mouse_user": mouse_user,
        }
        selected = {
            name: submitted_properties[name]
            for name, raw_value in provided.items()
            if raw_value is not None
        }
    try:
        updated_count = _db(request).batch_update_animal_properties(cage_id, selected)
    except ValueError as exc:
        return _redirect(
            request,
            f"/cages/{cage_id}#mice",
            str(exc),
            kind="error",
        )

    mouse_label = "mouse" if updated_count == 1 else "mice"
    property_label = "property" if len(selected) == 1 else "properties"
    return _redirect(
        request,
        f"/cages/{cage_id}#mice",
        f"Updated {len(selected)} {property_label} for {updated_count} {mouse_label}.",
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


@router.post("/surgeries/{surgery_id}/remove")
def remove_surgery(surgery_id: int, request: Request) -> RedirectResponse:
    database = _db(request)
    surgery = database.get_surgery(surgery_id)
    if surgery is None:
        raise HTTPException(status_code=404, detail="Surgery record not found.")
    cage_id = int(surgery["cage_id"])
    try:
        database.remove_surgery(surgery_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _redirect(request, f"/cages/{cage_id}", "Surgery record removed.")
