from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from shiritori.application import ApplicationServices
from shiritori.lexicon import LexiconCandidate, LexiconCode, LexiconResult
from shiritori.lobby import InMemoryLobbyRepository, LobbyService
from shiritori.rooms import (
    InMemoryRoomRepository,
    LexiconRoomService,
    RoomAuthorizationError,
    RoomCoordinator,
    RoomMode,
    RoomNotFound,
    WordSubmissionStatus,
    create_room_snapshot,
)
from shiritori.settings import Settings
from shiritori.themes import ThemeCatalog, ThemeDefinition


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def candidate(
    surface: str,
    reading: str,
    word_id: int,
) -> LexiconCandidate:
    return LexiconCandidate(
        surface=surface,
        reading=reading,
        lemma=surface,
        normalized_form=surface,
        part_of_speech=("名詞", "普通名詞", "一般", "*", "*", "*"),
        dictionary_id=0,
        word_id=word_id,
        canonical_key=reading,
    )


class StubLexicon:
    def __init__(self, result: LexiconResult) -> None:
        self.result = result

    def validate(self, _raw_surface: str | None) -> LexiconResult:
        return self.result


class ExplodingLexicon:
    def validate(self, _raw_surface: str | None) -> LexiconResult:
        raise AssertionError("unauthorized user reached dictionary validation")


class ThemeRoomIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_theme_filters_readings_before_commit(self) -> None:
        result = LexiconResult(
            code=LexiconCode.MULTIPLE_READINGS,
            surface="生物",
            message="読みを選んでください。",
            candidates=(
                candidate("生物", "せいぶつ", 1),
                candidate("生物", "なまもの", 2),
            ),
        )
        themes = ThemeCatalog(
            (
                ThemeDefinition.from_entries(
                    "food",
                    "食べ物",
                    (("生物", "なまもの"),),
                ),
            )
        )
        snapshot = create_room_snapshot(
            "themed-room",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            theme_key="food",
            now=NOW,
            seat_picker=lambda _: 0,
        )
        repository = InMemoryRoomRepository((snapshot,))
        service = LexiconRoomService(
            RoomCoordinator(repository),
            StubLexicon(result),
            themes=themes,
        )

        choice = await service.submit_user_word(
            snapshot.room_id,
            "alice",
            "生物",
            expected_version=0,
            operation_id="theme-choice",
            now=NOW,
        )
        rejected = await service.submit_user_word(
            snapshot.room_id,
            "alice",
            "生物",
            chosen_reading="せいぶつ",
            expected_version=0,
            operation_id="theme-wrong-reading",
            now=NOW,
        )
        committed = await service.submit_user_word(
            snapshot.room_id,
            "alice",
            "生物",
            chosen_reading="なまもの",
            expected_version=0,
            operation_id="theme-correct-reading",
            now=NOW,
        )

        self.assertEqual(choice.status, WordSubmissionStatus.READING_REQUIRED)
        self.assertEqual(choice.reading_choices, ("なまもの",))
        self.assertEqual(rejected.status, WordSubmissionStatus.REJECTED)
        self.assertEqual(committed.status, WordSubmissionStatus.COMMITTED)
        self.assertEqual(committed.selected_reading, "なまもの")

    async def test_outsider_is_rejected_before_dictionary_work(self) -> None:
        snapshot = create_room_snapshot(
            "protected-room",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            now=NOW,
            seat_picker=lambda _: 0,
        )
        service = LexiconRoomService(
            RoomCoordinator(InMemoryRoomRepository((snapshot,))),
            ExplodingLexicon(),
        )

        with self.assertRaises(RoomAuthorizationError):
            await service.submit_user_word(
                snapshot.room_id,
                "outsider",
                "林檎",
                expected_version=0,
                operation_id="outsider-submit",
                now=NOW,
            )

    async def test_unknown_persisted_theme_fails_closed(self) -> None:
        result = LexiconResult(
            code=LexiconCode.ACCEPTED,
            surface="林檎",
            message="辞書にあります。",
            candidates=(candidate("林檎", "りんご", 1),),
        )
        snapshot = create_room_snapshot(
            "unknown-theme-room",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            theme_key="missing",
            now=NOW,
            seat_picker=lambda _: 0,
        )
        service = LexiconRoomService(
            RoomCoordinator(InMemoryRoomRepository((snapshot,))),
            StubLexicon(result),
            themes=ThemeCatalog(),
        )

        response = await service.submit_user_word(
            snapshot.room_id,
            "alice",
            "林檎",
            expected_version=0,
            operation_id="unknown-theme",
            now=NOW,
        )

        self.assertEqual(response.status, WordSubmissionStatus.REJECTED)
        self.assertIn("テーマ", response.message)


class ServiceWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_coordinator_notifies_on_connect_and_commit(self) -> None:
        snapshot = create_room_snapshot(
            "notify-room",
            ("alice", "bob"),
            mode=RoomMode.PVP,
            now=NOW,
            seat_picker=lambda _: 0,
        )
        coordinator = RoomCoordinator(InMemoryRoomRepository((snapshot,)))
        notifications: list[str] = []
        coordinator.set_activity_notifier(notifications.append)

        await coordinator.connect_client(
            snapshot.room_id,
            "alice",
            "alice-tab",
            now=NOW,
        )
        await coordinator.submit_user_turn(
            snapshot.room_id,
            "alice",
            surface="林檎",
            reading="りんご",
            canonical_key="りんご",
            expected_version=0,
            operation_id="notify-submit",
            now=NOW,
        )

        self.assertEqual(
            notifications,
            [snapshot.room_id, snapshot.room_id],
        )

    async def test_lobby_rejects_unregistered_theme(self) -> None:
        themes = ThemeCatalog(
            (
                ThemeDefinition.from_entries(
                    "food",
                    "食べ物",
                    (("林檎", "りんご"),),
                ),
            )
        )
        service = LobbyService(
            InMemoryLobbyRepository(),
            code_factory=lambda: "ABCD23",
            theme_resolver=themes.get,
        )

        room = service.create_pvp_room(
            "owner",
            name="food room",
            theme_key="food",
        )
        self.assertEqual(room.theme_key, "food")
        with self.assertRaises(ValueError):
            service.create_pvp_room(
                "owner",
                name="unknown room",
                theme_key="unknown",
            )


class StartupRecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_start_rearms_absence_before_runtime(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(temporary_directory.name, "restart.sqlite3")
        database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        services = ApplicationServices.build(
            Settings(
                app_env="test",
                database_url=database_url,
                direct_database_url=database_url,
                nicegui_storage_secret=(
                    "test-storage-secret-with-32-characters"
                ),
                session_secret="test-session-secret-with-32-characters",
            )
        )
        try:
            services.database.create_schema_for_testing()
            owner = services.auth.register(
                "restart-owner",
                "owner-password-123",
            )
            guest = services.auth.register(
                "restart-guest",
                "guest-password-123",
            )
            lobby = services.lobby.create_pvp_room(
                owner.id,
                name="restart room",
            )
            services.lobby.join_as_player(guest.id, lobby.room_code)
            services.lobby.set_ready(
                owner.id,
                lobby.room_code,
                ready=True,
            )
            services.lobby.set_ready(
                guest.id,
                lobby.room_code,
                ready=True,
            )
            started = services.lobby.start(owner.id, lobby.room_code)
            services.rooms.disconnect_grace_seconds = 0

            await services.start()
            for _ in range(20):
                await asyncio.sleep(0)
                try:
                    await services.rooms.load_snapshot(started.game_id)
                except RoomNotFound:
                    break
            else:
                self.fail("empty PvP room was not removed after restart")

            for _ in range(100):
                if started.game_id not in services.runtime.active_room_ids:
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("deleted room supervisor did not stop")
        finally:
            await services.close()
            temporary_directory.cleanup()


if __name__ == "__main__":
    unittest.main()
