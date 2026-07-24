"""Validated application environment settings.

Production refuses to start with missing or weak secrets. Development uses an
explicitly named SQLite database and development-only secrets.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
import os


class SettingsError(RuntimeError):
    """Raised when the process environment is unsafe or incomplete."""


def _validate_production_secret(name: str, value: str) -> None:
    """Reject common placeholders and obviously non-random secrets."""

    if len(value) < 32:
        raise SettingsError(f"{name} must contain at least 32 characters")
    if any(character.isspace() for character in value):
        raise SettingsError(f"{name} must not contain whitespace")
    lowered = value.casefold()
    placeholder_markers = (
        "development-only",
        "change-before-deploy",
        "change-me",
        "changeme",
        "placeholder",
        "replace-me",
        "example-secret",
        "your-secret",
    )
    if any(marker in lowered for marker in placeholder_markers):
        raise SettingsError(f"{name} must not use a placeholder value")
    counts = Counter(value)
    if len(counts) < 10 or max(counts.values()) > len(value) // 2:
        raise SettingsError(f"{name} has insufficient character diversity")


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    database_url: str
    direct_database_url: str
    nicegui_storage_secret: str
    session_secret: str
    session_cookie_name: str = "siritori_session"
    csrf_cookie_name: str = "siritori_csrf"

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    @property
    def cookie_secure(self) -> bool:
        return self.production

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "Settings":
        values = os.environ if environ is None else environ
        app_env = values.get("APP_ENV", "development").strip().lower()
        if app_env not in {"development", "test", "production"}:
            raise SettingsError(
                "APP_ENV must be development, test, or production"
            )

        default_db = "sqlite+pysqlite:///./siritori-dev.db"
        database_url = values.get("DATABASE_URL", "").strip()
        direct_database_url = values.get("DIRECT_DATABASE_URL", "").strip()
        storage_secret = values.get("NICEGUI_STORAGE_SECRET", "")
        session_secret = values.get("SESSION_SECRET", "")

        if app_env == "production":
            missing = [
                name
                for name, value in (
                    ("DATABASE_URL", database_url),
                    ("DIRECT_DATABASE_URL", direct_database_url),
                    ("NICEGUI_STORAGE_SECRET", storage_secret),
                    ("SESSION_SECRET", session_secret),
                )
                if not value
            ]
            if missing:
                raise SettingsError(
                    "missing production settings: " + ", ".join(missing)
                )
            _validate_production_secret(
                "NICEGUI_STORAGE_SECRET", storage_secret
            )
            _validate_production_secret("SESSION_SECRET", session_secret)
            if storage_secret == session_secret:
                raise SettingsError(
                    "NICEGUI_STORAGE_SECRET and SESSION_SECRET must differ"
                )
            accepted_prefixes = (
                "postgres://",
                "postgresql://",
                "postgresql+psycopg://",
            )
            if not database_url.startswith(accepted_prefixes) or not (
                direct_database_url.startswith(accepted_prefixes)
            ):
                raise SettingsError(
                    "production database URLs must use PostgreSQL"
                )
            if database_url == direct_database_url:
                raise SettingsError(
                    "production pooled and direct database URLs must differ"
                )
        else:
            database_url = database_url or default_db
            direct_database_url = direct_database_url or database_url
            storage_secret = (
                storage_secret
                or "development-only-nicegui-secret-change-before-deploy"
            )
            session_secret = (
                session_secret
                or "development-only-session-secret-change-before-deploy"
            )

        return cls(
            app_env=app_env,
            database_url=database_url,
            direct_database_url=direct_database_url,
            nicegui_storage_secret=storage_secret,
            session_secret=session_secret,
        )


__all__ = ["Settings", "SettingsError"]