"""Application factory and localhost-only entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import LOCAL_BIND_HOST, Settings, get_settings
from app.database import Database
from app.importer import ImportReport, seed_if_empty
from app.routes import router
from app.security import add_security_middleware

APP_DIR = Path(__file__).resolve().parent


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    run_seed: bool | None = None,
) -> FastAPI:
    """Create the application without exposing it beyond loopback."""

    resolved_settings = settings or get_settings()
    resolved_database = database or Database(
        resolved_settings.database_path,
        room_aliases=resolved_settings.room_aliases,
        breeding_rooms=resolved_settings.breeding_room_names,
    )
    resolved_database.initialize()

    should_seed = resolved_settings.seed_on_empty if run_seed is None else run_seed
    seed_report = ImportReport()
    if should_seed:
        seed_report = seed_if_empty(
            resolved_database,
            csv_path=resolved_settings.seed_csv_path,
            xlsx_path=resolved_settings.seed_xlsx_path,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            resolved_database.close()

    app = FastAPI(
        title="Mouse Colony Management GUI",
        root_path=resolved_settings.root_path,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved_settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.seed_report = seed_report
    app.state.templates = Jinja2Templates(directory=APP_DIR / "templates")

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=resolved_settings.trusted_hosts,
        www_redirect=False,
    )
    add_security_middleware(app, allowed_hosts=resolved_settings.trusted_hosts)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    app.include_router(router)
    return app


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=LOCAL_BIND_HOST,
        port=settings.local_port,
        root_path=settings.root_path,
        log_level=settings.log_level,
        access_log=False,
    )


if __name__ == "__main__":
    run()
