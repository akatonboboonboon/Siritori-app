"""Apply the schema and start NiceGUI.

Render Free has no pre-deploy command, so the single web process performs the
idempotent Alembic upgrade before it starts accepting traffic.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from shiritori.settings import Settings


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def migrate() -> None:
    settings = Settings.from_environment()
    os.environ.setdefault(
        "DIRECT_DATABASE_URL", settings.direct_database_url
    )
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(config, "head")


def start() -> None:
    migrate()
    from main import run

    run()


if __name__ == "__main__":
    start()