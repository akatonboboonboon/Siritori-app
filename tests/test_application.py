from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from shiritori.application import ApplicationServices
from shiritori.bots import BotContext, BotStrategy, WordIndex, WordOption
from shiritori.settings import Settings
from shiritori.themes import ThemeDefinition


class StubEasyBot:
    def choose(
        self,
        context: BotContext,
        words: WordIndex,
    ) -> WordOption | None:
        options = words.legal_options(
            context.expected_kana,
            context.used_canonical_keys,
        )
        return options[0] if options else None


class ApplicationServicesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(
            self.temporary_directory.name,
            "application.sqlite3",
        )
        url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        settings = Settings(
            app_env="test",
            database_url=url,
            direct_database_url=url,
            nicegui_storage_secret="test-storage-secret-with-32-characters",
            session_secret="test-session-secret-with-32-characters",
        )
        self.services = ApplicationServices.build(settings)
        self.services.database.create_schema_for_testing()

    async def asyncTearDown(self) -> None:
        await self.services.close()
        self.temporary_directory.cleanup()

    async def test_start_is_idempotent_without_active_rooms(self) -> None:
        await self.services.start()
        await self.services.start()

        self.assertEqual(self.services.runtime.active_room_ids, frozenset())

    async def test_all_difficulties_are_registered_and_replaceable(self) -> None:
        self.assertIsInstance(
            self.services.bot_strategy_for("easy"),
            BotStrategy,
        )
        self.assertIsInstance(
            self.services.bot_strategy_for("normal"),
            BotStrategy,
        )
        self.assertIsInstance(
            self.services.bot_strategy_for("hard"),
            BotStrategy,
        )

        easy = StubEasyBot()
        with self.assertRaises(ValueError):
            self.services.register_bot_strategy("easy", easy)
        self.services.register_bot_strategy("easy", easy, replace=True)
        self.assertIs(self.services.bot_strategy_for("easy"), easy)

    async def test_custom_theme_builds_a_validated_bot_index(self) -> None:
        theme = ThemeDefinition.from_entries(
            "fruit",
            "果物",
            [("林檎", "りんご"), ("ゴリラ", "ごりら")],
        )
        self.services.register_theme(theme)

        index = self.services.word_index_for("fruit")

        self.assertTrue(index.starting_with("り"))
        self.assertIs(index, self.services.word_index_for("fruit"))


if __name__ == "__main__":
    unittest.main()
