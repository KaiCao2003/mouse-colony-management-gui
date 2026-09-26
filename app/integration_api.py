"""Token-authenticated, loopback-only read API for planning applications."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.database import AmbiguousAnimalIdentifierError, Database

API_SCHEMA_VERSION: Final[int] = 1
API_PREFIX: Final[str] = "/api/v1"

router = APIRouter(prefix=API_PREFIX, include_in_schema=False)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"schemaVersion": API_SCHEMA_VERSION, "code": code, "detail": detail},
    )


def _authorize(request: Request) -> JSONResponse | None:
    configured = request.app.state.settings.integration_token
    if configured is None:
        return _error(503, "integration_disabled", "The subject integration API is disabled.")

    candidates = request.headers.getlist("authorization")
    if len(candidates) != 1:
        return _error(401, "invalid_token", "A bearer token is required.")
    scheme, separator, candidate = candidates[0].partition(" ")
    if (
        separator != " "
        or scheme.casefold() != "bearer"
        or not candidate
        or len(candidate) > 512
        or not hmac.compare_digest(configured.get_secret_value(), candidate)
    ):
        return _error(401, "invalid_token", "A bearer token is required.")
    return None


def _subject_payload(record: dict[str, Any]) -> dict[str, Any]:
    surgeries = [
        {
            "date": surgery["surgery_date"],
            "time": surgery["surgery_time"],
            "operator": surgery["operator"],
            "type": surgery["surgery_type"],
        }
        for surgery in record["surgeries"]
    ]
    return {
        "publicId": record["public_id"],
        "legacyId": record["legacy_id"],
        "status": record["status"],
        "sex": record["sex"],
        "dateOfBirth": record["dob"],
        "genotype": record["genotype"],
        "mouseUser": record["mouse_user"],
        "updatedAt": record["updated_at"],
        "cage": {
            "cageCardId": record["cage_card_id"],
            "status": record["cage_status"],
            "room": record["cage_room"],
            "protocol": record["cage_protocol"],
        },
        "surgeries": surgeries,
    }


@router.get("/health", response_model=None)
def integration_health(request: Request) -> dict[str, object] | JSONResponse:
    denied = _authorize(request)
    if denied is not None:
        return denied
    return {
        "schemaVersion": API_SCHEMA_VERSION,
        "service": "mouse_line",
        "status": "ok",
        "capabilities": ["subject.read"],
    }


@router.get("/animals/resolve", response_model=None)
def resolve_animal(
    request: Request,
    identifier: str = Query(min_length=1, max_length=100),
) -> dict[str, object] | JSONResponse:
    denied = _authorize(request)
    if denied is not None:
        return denied

    database: Database = request.app.state.database
    try:
        resolved = database.resolve_animal_identifier(identifier)
    except AmbiguousAnimalIdentifierError as exc:
        return _error(409, "ambiguous_legacy_id", str(exc))
    except ValueError as exc:
        return _error(422, "invalid_identifier", str(exc))
    if resolved is None:
        return _error(404, "subject_not_found", "No mouse has that exact identifier.")

    record, matched_by = resolved
    subject = _subject_payload(record)
    receipt_material = {
        "schemaVersion": API_SCHEMA_VERSION,
        "service": "mouse_line",
        "subject": subject,
    }
    return {
        **receipt_material,
        "queryIdentifier": identifier,
        "matchedBy": "publicId" if matched_by == "public_id" else "legacyId",
        "subjectRecordSha256": _canonical_sha256(receipt_material),
        "usableForNavigation": False,
    }
