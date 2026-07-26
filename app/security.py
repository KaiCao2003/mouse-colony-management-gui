"""Small local-request guard for the shared, no-login application."""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from collections.abc import Collection
from typing import Any, Final
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_HEADER_NAME: Final[str] = "X-CSRF-Token"
STATE_CHANGING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SECURITY_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "; ".join(
        (
            "default-src 'self'",
            "base-uri 'none'",
            "connect-src 'self'",
            "font-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "img-src 'self' data:",
            "object-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
        )
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class CsrfTokenManager:
    def __init__(self) -> None:
        self._token = secrets.token_urlsafe(32)

    def issue_token(self) -> str:
        return self._token

    def validate(self, candidate: str | None) -> bool:
        if candidate is None:
            return False
        return hmac.compare_digest(self._token, candidate)


def csrf_token_for_request(request: Any) -> str:
    return request.app.state.csrf_manager.issue_token()


def _is_loopback(value: str | None) -> bool:
    if not value:
        return False
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _host_name(value: str | None) -> str | None:
    if not value or any(char in value for char in "\r\n/?#@,"):
        return None
    candidate = value.strip()
    if candidate.startswith("["):
        end = candidate.find("]")
        return candidate[1:end].casefold() if end > 0 else None
    return candidate.rsplit(":", 1)[0].casefold().rstrip(".")


def _same_origin(headers: Headers, scheme: str) -> bool:
    host = headers.get("host")
    expected_host = _host_name(host)
    if expected_host is None:
        return False
    origin = headers.get("origin")
    referer = headers.get("referer")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if (
            parsed.scheme.casefold() != scheme.casefold()
            or (parsed.hostname or "").casefold().rstrip(".") != expected_host
            or parsed.path not in {"", "/"}
        ):
            return False
    if referer:
        try:
            parsed = urlsplit(referer)
        except ValueError:
            return False
        if (
            parsed.scheme.casefold() != scheme.casefold()
            or (parsed.hostname or "").casefold().rstrip(".") != expected_host
        ):
            return False
    if origin or referer:
        return True
    return headers.get("sec-fetch-site", "").casefold() == "same-origin"


def _error(code: str) -> JSONResponse:
    response = JSONResponse(
        {"detail": "The local request security check failed.", "code": code},
        status_code=403,
    )
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


class LocalGuardMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        csrf_manager: CsrfTokenManager,
        allowed_hosts: Collection[str],
    ) -> None:
        self.app = app
        self.csrf_manager = csrf_manager
        self.allowed_hosts = frozenset(host.casefold().rstrip(".") for host in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        client = scope.get("client")
        if not _is_loopback(client[0] if client else None):
            await _error("non_loopback_client")(scope, receive, send)
            return
        if _host_name(headers.get("host")) not in self.allowed_hosts:
            await _error("invalid_host")(scope, receive, send)
            return
        if scope.get("method", "GET").upper() in STATE_CHANGING_METHODS:
            if not _same_origin(headers, scope.get("scheme", "http")):
                await _error("invalid_origin")(scope, receive, send)
                return
            candidates = headers.getlist(CSRF_HEADER_NAME)
            if len(candidates) != 1 or not self.csrf_manager.validate(candidates[0]):
                await _error("invalid_csrf")(scope, receive, send)
                return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, wrapped)


def add_security_middleware(app: Any, *, allowed_hosts: Collection[str]) -> CsrfTokenManager:
    manager = CsrfTokenManager()
    app.state.csrf_manager = manager
    app.add_middleware(
        LocalGuardMiddleware,
        csrf_manager=manager,
        allowed_hosts=allowed_hosts,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return manager
