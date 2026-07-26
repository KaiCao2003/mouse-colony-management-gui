"""Local-only configuration for the mouseline manager."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_BIND_HOST: Final[str] = "127.0.0.1"
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Settings loaded once when the local process starts."""

    model_config = SettingsConfigDict(
        env_prefix="MOUSELINE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    local_port: int = Field(default=8765, ge=1024, le=65535)
    database_path: Path = PROJECT_ROOT / "data" / "mouseline.db"
    seed_csv_path: Path = PROJECT_ROOT / "seed" / "cage-cards.csv"
    seed_xlsx_path: Path = PROJECT_ROOT / "seed" / "mouse-line.xlsx"
    seed_on_empty: bool = True
    docs_enabled: bool = False
    log_level: str = "info"
    root_path: str = ""
    allowed_hosts: str = "127.0.0.1,localhost,testserver"
    room_aliases_json: str = "{}"
    breeding_rooms: str = ""

    @field_validator("root_path")
    @classmethod
    def normalize_root_path(cls, value: str) -> str:
        """Normalize an optional reverse-proxy mount such as ``/colony``."""

        candidate = value.strip()
        if candidate in {"", "/"}:
            return ""
        candidate = f"/{candidate.lstrip('/').rstrip('/')}"
        if not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)+", candidate):
            raise ValueError("root_path must contain URL-safe path segments")
        return candidate

    @field_validator("room_aliases_json")
    @classmethod
    def validate_room_aliases_json(cls, value: str) -> str:
        try:
            aliases = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("room_aliases_json must be valid JSON") from exc
        if not isinstance(aliases, dict) or any(
            not isinstance(key, str) or not isinstance(alias, str) for key, alias in aliases.items()
        ):
            raise ValueError("room_aliases_json must be a JSON object of strings")
        return value

    @property
    def trusted_hosts(self) -> list[str]:
        """Return exact host names accepted from the local reverse proxy."""

        hosts = [host.strip().casefold().rstrip(".") for host in self.allowed_hosts.split(",")]
        return list(dict.fromkeys(host for host in hosts if host))

    @property
    def room_aliases(self) -> dict[str, str]:
        aliases = json.loads(self.room_aliases_json)
        assert isinstance(aliases, dict)
        return {
            key.strip(): alias.strip()
            for key, alias in aliases.items()
            if key.strip() and alias.strip()
        }

    @property
    def breeding_room_names(self) -> list[str]:
        rooms = [room.strip() for room in self.breeding_rooms.split(",")]
        return list(dict.fromkeys(room for room in rooms if room))


@lru_cache
def get_settings() -> Settings:
    return Settings()
