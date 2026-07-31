"""Local request, login-session, and response security for the application."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
import unicodedata
from collections.abc import Collection
from http.cookies import CookieError, SimpleCookie
from typing import Any, Final, Protocol
from urllib.parse import SplitResult, urlencode, urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_HEADER_NAME: Final[str] = "X-CSRF-Token"
LOGIN_COOKIE_NAME: Final[str] = "mouseline_session"
LOGIN_SESSION_MAX_AGE_SECONDS: Final[int] = 30 * 24 * 60 * 60
STATE_CHANGING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOGIN_ANSWER_DIGEST: Final[bytes] = bytes.fromhex(
    "REDACTED_LOGIN_ANSWER_DIGEST"
)
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


def _route_path(scope: Scope) -> str:
    path = str(scope.get("path", "/"))
    root_path = str(scope.get("root_path", "")).rstrip("/")
    if root_path and (path == root_path or path.startswith(f"{root_path}/")):
        return path[len(root_path) :] or "/"
    return path


class LoginSessionStore(Protocol):
    """Persistence contract for hashed login sessions."""

    def create_login_session(
        self,
        token_digest: bytes,
        expires_at: int,
        *,
        now: int,
    ) -> None: ...

    def validate_login_session(self, token_digest: bytes, *, now: int) -> bool: ...

    def delete_login_session(self, token_digest: bytes) -> None: ...


class LoginManager:
    """Validate the shared answer and persistent, unguessable session tokens."""

    def __init__(self, session_store: LoginSessionStore) -> None:
        self._session_store = session_store

    @staticmethod
    def _normalized_answer(candidate: str) -> str:
        return unicodedata.normalize("NFKC", candidate).strip().casefold()

    def validate_answer(self, candidate: str) -> bool:
        if len(candidate) > 100:
            return False
        candidate_digest = hashlib.sha256(
            self._normalized_answer(candidate).encode("utf-8")
        ).digest()
        return hmac.compare_digest(_LOGIN_ANSWER_DIGEST, candidate_digest)

    @staticmethod
    def _token_digest(candidate: str) -> bytes:
        return hashlib.sha256(candidate.encode("utf-8")).digest()

    def issue_session_token(self) -> str:
        token = secrets.token_urlsafe(48)
        now = int(time.time())
        self._session_store.create_login_session(
            self._token_digest(token),
            now + LOGIN_SESSION_MAX_AGE_SECONDS,
            now=now,
        )
        return token

    def validate_session(self, candidate: str | None) -> bool:
        if candidate is None or len(candidate) > 256:
            return False
        return self._session_store.validate_login_session(
            self._token_digest(candidate),
            now=int(time.time()),
        )

    def revoke_session(self, candidate: str | None) -> None:
        if candidate is None or len(candidate) > 256:
            return
        self._session_store.delete_login_session(self._token_digest(candidate))


def _session_cookie(headers: Headers) -> str | None:
    raw_cookies = headers.getlist("cookie")
    if not raw_cookies:
        return None
    cookies = SimpleCookie()
    try:
        for raw_cookie in raw_cookies:
            cookies.load(raw_cookie)
    except CookieError:
        return None
    session = cookies.get(LOGIN_COOKIE_NAME)
    return None if session is None else session.value


class LoginRequiredMiddleware:
    """Redirect unauthenticated application requests to the shared login page."""

    def __init__(self, app: ASGIApp, login_manager: LoginManager) -> None:
        self.app = app
        self.login_manager = login_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        route_path = _route_path(scope)
        if route_path == "/login" or route_path.startswith("/static/"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if self.login_manager.validate_session(_session_cookie(headers)):
            await self.app(scope, receive, send)
            return

        root_path = str(scope.get("root_path", "")).rstrip("/")
        requested_path = f"{root_path}{route_path}"
        query_string = bytes(scope.get("query_string", b"")).decode("latin-1")
        if query_string:
            requested_path = f"{requested_path}?{query_string}"
        login_path = f"{root_path}/login"
        response = RedirectResponse(
            f"{login_path}?{urlencode({'next': requested_path})}",
            status_code=303,
        )
        if _session_cookie(headers) is not None:
            response.delete_cookie(LOGIN_COOKIE_NAME, path=root_path or "/")
        await response(scope, receive, send)


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


def _origin_identity(parsed: SplitResult) -> tuple[str, str, int] | None:
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold().rstrip(".")
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def _same_origin(headers: Headers, scheme: str) -> bool:
    fetch_site = headers.get("sec-fetch-site", "").casefold()
    if fetch_site == "same-origin":
        return True
    if fetch_site in {"cross-site", "same-site"}:
        return False

    host = headers.get("host")
    if _host_name(host) is None:
        return False
    try:
        expected = _origin_identity(urlsplit(f"{scheme}://{host}"))
    except ValueError:
        return False
    if expected is None:
        return False
    origin = headers.get("origin")
    referer = headers.get("referer")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        if _origin_identity(parsed) != expected or parsed.path not in {"", "/"}:
            return False
    if referer:
        try:
            parsed = urlsplit(referer)
        except ValueError:
            return False
        if _origin_identity(parsed) != expected:
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
            if _route_path(scope) != "/login":
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


def add_security_middleware(
    app: Any,
    *,
    allowed_hosts: Collection[str],
    session_store: LoginSessionStore,
) -> CsrfTokenManager:
    csrf_manager = CsrfTokenManager()
    login_manager = LoginManager(session_store)
    app.state.csrf_manager = csrf_manager
    app.state.login_manager = login_manager
    app.add_middleware(
        LoginRequiredMiddleware,
        login_manager=login_manager,
    )
    app.add_middleware(
        LocalGuardMiddleware,
        csrf_manager=csrf_manager,
        allowed_hosts=allowed_hosts,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return csrf_manager
