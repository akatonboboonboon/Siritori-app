"""NiceGUI application entry point for local development and Render."""

from __future__ import annotations

import os

from fastapi import HTTPException, Request
from nicegui import app, ui
from sqlalchemy import text

from shiritori.admin_pages import register_admin_pages
from shiritori.application import ApplicationServices
from shiritori.customize import APP_TITLE
from shiritori.daily_pages import register_daily_pages
from shiritori.page import register_pages
from shiritori.settings import Settings
from shiritori.web_auth import AuthWebServices, register_auth_pages


SETTINGS = Settings.from_environment()
SERVICES = ApplicationServices.build(SETTINGS)
DATABASE = SERVICES.database
AUTH = SERVICES.auth
LOBBY = SERVICES.lobby
ROOMS = SERVICES.rooms
ROOM_WORDS = SERVICES.room_words
ROOM_RUNTIME = SERVICES.runtime
SOLO = SERVICES.solo
STATISTICS = SERVICES.statistics
SCORE_ATTACK = SERVICES.score_attack
WORD_SUGGESTIONS = SERVICES.word_suggestions
DAILY_CHALLENGE = SERVICES.daily_challenge
ONBOARDING = SERVICES.onboarding
WORD_REVIEW = SERVICES.word_review


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith(
        (
            "/auth/",
            "/login",
            "/register",
            "/lobby",
            "/saved-games",
            "/play/",
            "/join/",
            "/room/",
            "/stats",
            "/rankings",
            "/score-attack",
            "/word-suggestions",
            "/tutorial",
            "/daily-challenge",
            "/admin/word-suggestions",
        )
    ):
        response.headers.setdefault("Cache-Control", "no-store")
    if SETTINGS.production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000"
        )
    return response


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness endpoint used by Render's health check."""

    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    """Verify that the authoritative database is reachable."""

    try:
        with DATABASE.read_session() as session:
            session.execute(text("SELECT 1"))
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="database unavailable"
        ) from error
    return {"status": "ready"}


register_pages(SERVICES.approved_validator)
register_auth_pages(
    AuthWebServices(
        auth=AUTH,
        games=SERVICES.games,
        settings=SETTINGS,
        solo=SOLO,
        rooms=ROOMS,
        room_words=ROOM_WORDS,
        lobby=LOBBY,
        statistics=STATISTICS,
        score_attack=SCORE_ATTACK,
        word_suggestions=WORD_SUGGESTIONS,
        word_review=WORD_REVIEW,
        onboarding=ONBOARDING,
    )
)
register_admin_pages(
    auth=AUTH,
    settings=SETTINGS,
    word_review=WORD_REVIEW,
)
register_daily_pages(
    auth=AUTH,
    settings=SETTINGS,
    daily_challenge=DAILY_CHALLENGE,
)
app.on_startup(SERVICES.start)
app.on_shutdown(SERVICES.close)


def run() -> None:
    """Run the NiceGUI web server locally or on Render."""

    ui.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        title=APP_TITLE,
        favicon="🔗",
        language="ja",
        storage_secret=SETTINGS.nicegui_storage_secret,
        session_middleware_kwargs={
            "https_only": SETTINGS.cookie_secure,
            "same_site": "lax",
        },
        reload=os.getenv("NICEGUI_RELOAD", "").lower()
        in {"1", "true", "yes"},
        show=False,
        show_welcome_message=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    run()
