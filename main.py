"""NiceGUI application entry point.

Render starts this file with ``python main.py``.  Keeping the server
configuration here makes the game logic importable without starting a server.
"""

from __future__ import annotations

import os

from nicegui import app, ui

from shiritori.customize import APP_TITLE
from shiritori.page import register_pages


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Small endpoint used by Render's health check."""

    return {"status": "ok"}


register_pages()


def run() -> None:
    """Run the NiceGUI web server locally or on Render."""

    ui.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        title=APP_TITLE,
        favicon="🔗",
        language="ja",
        reload=os.getenv("NICEGUI_RELOAD", "").lower() in {"1", "true", "yes"},
        show=False,
        show_welcome_message=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    run()
