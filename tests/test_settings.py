from __future__ import annotations

import unittest

from shiritori.settings import Settings, SettingsError


STORAGE_SECRET = "N7!ceGUI-Storage_2026:aB3dE5fG8hJ"
SESSION_SECRET = "Sess10n-Key_2026:Zx9Yw8Vu7Ts6Rq5P"


class SettingsTests(unittest.TestCase):
    def test_development_has_local_defaults(self) -> None:
        settings = Settings.from_environment({})

        self.assertEqual(settings.app_env, "development")
        self.assertTrue(settings.database_url.startswith("sqlite"))
        self.assertFalse(settings.cookie_secure)

    def test_production_requires_every_secret_and_database_url(self) -> None:
        with self.assertRaises(SettingsError):
            Settings.from_environment({"APP_ENV": "production"})

    def test_render_is_production_even_without_app_env(self) -> None:
        with self.assertRaises(SettingsError):
            Settings.from_environment({"RENDER": "true"})

        settings = Settings.from_environment(
            {
                "RENDER": "true",
                "DATABASE_URL": "postgresql://pooled",
                "DIRECT_DATABASE_URL": "postgresql://direct",
                "NICEGUI_STORAGE_SECRET": STORAGE_SECRET,
                "SESSION_SECRET": SESSION_SECRET,
            }
        )

        self.assertEqual(settings.app_env, "production")
        self.assertTrue(settings.cookie_secure)

    def test_production_requires_distinct_long_secrets(self) -> None:
        base = {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://pooled",
            "DIRECT_DATABASE_URL": "postgresql://direct",
            "NICEGUI_STORAGE_SECRET": STORAGE_SECRET,
            "SESSION_SECRET": STORAGE_SECRET,
        }
        with self.assertRaises(SettingsError):
            Settings.from_environment(base)

        valid = dict(base, SESSION_SECRET=SESSION_SECRET)
        settings = Settings.from_environment(valid)
        self.assertTrue(settings.cookie_secure)

    def test_production_rejects_non_postgres_or_same_endpoints(self) -> None:
        base = {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://one",
            "DIRECT_DATABASE_URL": "postgresql://two",
            "NICEGUI_STORAGE_SECRET": STORAGE_SECRET,
            "SESSION_SECRET": SESSION_SECRET,
        }
        with self.assertRaises(SettingsError):
            Settings.from_environment(
                dict(base, DATABASE_URL="sqlite+pysqlite:///bad.db")
            )
        with self.assertRaises(SettingsError):
            Settings.from_environment(
                dict(base, DIRECT_DATABASE_URL="postgresql://one")
            )

    def test_production_rejects_placeholder_whitespace_and_low_diversity(self) -> None:
        base = {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://one",
            "DIRECT_DATABASE_URL": "postgresql://two",
            "NICEGUI_STORAGE_SECRET": STORAGE_SECRET,
            "SESSION_SECRET": SESSION_SECRET,
        }
        unsafe_values = (
            "development-only-session-secret-change-before-deploy",
            "Change-Me-Please-1234567890-ABCDEFGHIJK",
            "Diverse-looking-but contains whitespace-1234",
            "x" * 64,
            "abcd" * 16,
        )
        for unsafe in unsafe_values:
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(SettingsError):
                    Settings.from_environment(
                        dict(base, SESSION_SECRET=unsafe)
                    )


if __name__ == "__main__":
    unittest.main()
