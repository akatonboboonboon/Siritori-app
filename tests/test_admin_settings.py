"""Tests for the optional administrator username allowlist."""

from __future__ import annotations

import unittest

from shiritori.settings import Settings, SettingsError


class AdminSettingsTests(unittest.TestCase):
    def test_admin_usernames_are_nfkc_casefolded_and_deduplicated(self) -> None:
        settings = Settings.from_environment(
            {
                "APP_ENV": "test",
                "ADMIN_USERNAMES": " Owner,ＯＷＮＥＲ, 審査員 ",
            }
        )

        self.assertEqual(
            settings.admin_username_keys,
            frozenset({"owner", "審査員"}),
        )

    def test_empty_allowlist_keeps_admin_review_disabled(self) -> None:
        settings = Settings.from_environment(
            {"APP_ENV": "test", "ADMIN_USERNAMES": " , "}
        )

        self.assertEqual(settings.admin_username_keys, frozenset())

    def test_malformed_or_excessive_admin_allowlist_is_rejected(self) -> None:
        with self.assertRaises(SettingsError):
            Settings.from_environment(
                {"APP_ENV": "test", "ADMIN_USERNAMES": "ab"}
            )

        too_many = ",".join(f"admin{index:02d}" for index in range(21))
        with self.assertRaises(SettingsError):
            Settings.from_environment(
                {"APP_ENV": "test", "ADMIN_USERNAMES": too_many}
            )


if __name__ == "__main__":
    unittest.main()
