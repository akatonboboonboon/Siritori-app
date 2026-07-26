from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from shiritori.database import Database
from shiritori.game_session import GameSession, SessionCode
from shiritori.lexicon import (
    LexiconCandidate,
    LexiconCode,
    LexiconResult,
    normalize_surface,
)
from shiritori.models import ApprovedWord, User, new_id
from shiritori.word_review import (
    ApprovedLexiconValidator,
    ApprovedWordCatalog,
)


BASE_TIME = datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc)


def rejected(
    surface: str,
    code: LexiconCode = LexiconCode.NOT_IN_DICTIONARY,
) -> LexiconResult:
    return LexiconResult(
        code=code,
        surface=surface,
        message=f"base:{code.value}",
    )


def accepted(
    surface: str,
    reading: str,
    *,
    word_id: int = 1,
) -> LexiconResult:
    candidate = LexiconCandidate(
        surface=surface,
        reading=reading,
        lemma=surface,
        normalized_form=surface,
        part_of_speech=("名詞", "普通名詞", "一般", "*", "*", "*"),
        dictionary_id=0,
        word_id=word_id,
        canonical_key=reading,
    )
    return LexiconResult(
        code=LexiconCode.ACCEPTED,
        surface=surface,
        message="base:accepted",
        candidates=(candidate,),
    )


class StubBaseValidator:
    def __init__(
        self,
        results: dict[str, LexiconResult] | None = None,
    ) -> None:
        self.results = results or {}
        self.calls: list[str] = []

    def validate(self, raw_surface: str | None) -> LexiconResult:
        surface = normalize_surface(raw_surface)
        self.calls.append(surface)
        return self.results.get(surface, rejected(surface))


class ApprovedLexiconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(
            self.temporary_directory.name,
            "approved-lexicon.sqlite3",
        )
        self.database = Database(
            f"sqlite+pysqlite:///{database_path.as_posix()}"
        )
        self.database.create_schema_for_testing()
        with self.database.transaction() as session:
            self.admin = User(
                id=new_id(),
                username="admin",
                username_key="admin",
                display_name="管理者",
                password_hash="$argon2id$test-only",
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
            )
            session.add(self.admin)
        self.catalog = ApprovedWordCatalog(self.database)

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def _validator(
        self,
        results: dict[str, LexiconResult] | None = None,
    ) -> tuple[ApprovedLexiconValidator, StubBaseValidator]:
        base = StubBaseValidator(results)
        # The production object is protocol-shaped; a test double avoids
        # loading Sudachi for behavior that is independent of its data.
        validator = ApprovedLexiconValidator(
            self.catalog,
            base,  # type: ignore[arg-type]
        )
        return validator, base

    def test_sudachi_accepted_result_wins_over_catalog(self) -> None:
        self.catalog.add(
            word_id="approved-alternate",
            surface="上手",
            reading="うわて",
        )
        base_result = accepted("上手", "じょうず")
        validator, _ = self._validator({"上手": base_result})

        result = validator.validate("上手")

        self.assertIs(result, base_result)
        self.assertEqual(result.readings, ("じょうず",))

    def test_structural_rejections_never_reach_approved_fallback(self) -> None:
        self.catalog.add(
            word_id="structural-word",
            surface="承認語",
            reading="しょうにんご",
        )
        structural_codes = (
            LexiconCode.TOO_LONG,
            LexiconCode.INTERNAL_WHITESPACE,
            LexiconCode.INVALID_CHARACTERS,
            LexiconCode.SINGLE_HIRAGANA,
            LexiconCode.SINGLE_KATAKANA,
        )
        for code in structural_codes:
            with self.subTest(code=code):
                base_result = rejected("承認語", code)
                validator, _ = self._validator(
                    {"承認語": base_result}
                )
                self.assertIs(
                    validator.validate("承認語"),
                    base_result,
                )

    def test_exact_approved_fallback_builds_server_owned_candidate(self) -> None:
        entry = self.catalog.add(
            word_id="approved-tsukudani",
            surface="佃煮",
            reading="つくだに",
        )
        validator, base = self._validator()

        result = validator.validate("佃煮")

        self.assertEqual(base.calls, ["佃煮"])
        self.assertEqual(result.code, LexiconCode.ACCEPTED)
        self.assertEqual(result.surface, "佃煮")
        self.assertEqual(result.readings, ("つくだに",))
        candidate = result.candidates[0]
        self.assertEqual(candidate.canonical_key, "つくだに")
        self.assertEqual(candidate.lemma, "佃煮")
        self.assertEqual(candidate.part_of_speech[:2], ("名詞", "普通名詞"))
        self.assertEqual(candidate.word_id, entry.word_id)
        self.assertGreaterEqual(candidate.dictionary_id, 0)

    def test_fallback_applies_to_supported_dictionary_failure_codes(self) -> None:
        self.catalog.add(
            word_id="approved-noun",
            surface="承認語",
            reading="しょうにんご",
        )
        for code in (
            LexiconCode.NOT_IN_DICTIONARY,
            LexiconCode.UNSUPPORTED_PART_OF_SPEECH,
            LexiconCode.NO_USABLE_READING,
        ):
            with self.subTest(code=code):
                validator, _ = self._validator(
                    {"承認語": rejected("承認語", code)}
                )
                self.assertEqual(
                    validator.validate("承認語").code,
                    LexiconCode.ACCEPTED,
                )

    def test_unknown_or_rejected_word_remains_rejected(self) -> None:
        validator, _ = self._validator()

        result = validator.validate("未承認語")

        self.assertEqual(result.code, LexiconCode.NOT_IN_DICTIONARY)
        self.assertFalse(result.is_dictionary_word)

    def test_multiple_approved_readings_require_explicit_choice(self) -> None:
        self.catalog.add(
            word_id="approved-jouzu",
            surface="上手",
            reading="じょうず",
        )
        self.catalog.add(
            word_id="approved-uwate",
            surface="上手",
            reading="うわて",
        )
        validator, _ = self._validator()

        result = validator.validate("上手")

        self.assertEqual(result.code, LexiconCode.MULTIPLE_READINGS)
        self.assertEqual(result.readings, ("うわて", "じょうず"))
        self.assertEqual(
            tuple(
                candidate.reading
                for candidate in result.candidates_for_reading("じょうず")
            ),
            ("じょうず",),
        )

    def test_catalog_lookup_uses_the_same_unicode_normalization(self) -> None:
        self.catalog.add(
            word_id="approved-katakana",
            surface="ﾂｸﾀﾞﾆ",
            reading="つくだに",
        )
        validator, _ = self._validator()

        result = validator.validate("ﾂｸﾀﾞﾆ")

        self.assertEqual(result.surface, "ツクダニ")
        self.assertEqual(result.code, LexiconCode.ACCEPTED)

    def test_game_session_applies_chain_duplicate_and_ends_with_n_rules(
        self,
    ) -> None:
        self.catalog.add(
            word_id="approved-aa-one",
            surface="亜亜",
            reading="ああ",
        )
        self.catalog.add(
            word_id="approved-aa-two",
            surface="阿阿",
            reading="ああ",
        )
        self.catalog.add(
            word_id="approved-men",
            surface="麺",
            reading="めん",
        )
        validator, _ = self._validator()

        duplicate_game = GameSession(validator)  # type: ignore[arg-type]
        first = duplicate_game.submit("亜亜")
        duplicate = duplicate_game.submit("阿阿")
        ending_game = GameSession(validator)  # type: ignore[arg-type]
        ending = ending_game.submit("麺")

        self.assertEqual(first.code, SessionCode.ACCEPTED)
        self.assertEqual(duplicate.code, SessionCode.DUPLICATE)
        self.assertTrue(duplicate.game_over)
        self.assertEqual(ending.code, SessionCode.ENDS_WITH_N)
        self.assertTrue(ending.game_over)

    def test_refresh_atomically_replaces_catalog_and_skips_malformed_rows(
        self,
    ) -> None:
        with self.database.transaction() as session:
            session.add_all(
                (
                    ApprovedWord(
                        id="approved-valid",
                        surface="佃煮",
                        reading="つくだに",
                        approved_by_user_id=self.admin.id,
                        source_suggestion_id=None,
                        approved_at=BASE_TIME,
                    ),
                    ApprovedWord(
                        id="approved-invalid",
                        surface="壊語",
                        reading="not-hiragana",
                        approved_by_user_id=self.admin.id,
                        source_suggestion_id=None,
                        approved_at=BASE_TIME,
                    ),
                )
            )

        count = self.catalog.refresh()

        self.assertEqual(count, 1)
        self.assertEqual(self.catalog.entry_count, 1)
        self.assertEqual(
            self.catalog.lookup("佃煮")[0].reading,
            "つくだに",
        )
        self.assertEqual(self.catalog.lookup("壊語"), ())

    def test_copy_on_write_catalog_is_safe_under_concurrent_access(self) -> None:
        readings = ("かか", "かき", "かく", "かけ", "かこ")

        def add_and_read(index: int) -> tuple[str, ...]:
            reading = readings[index % len(readings)]
            self.catalog.add(
                word_id=f"approved-{index % len(readings)}",
                surface="仮語",
                reading=reading,
            )
            return tuple(
                entry.reading
                for entry in self.catalog.lookup("仮語")
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            snapshots = tuple(executor.map(add_and_read, range(100)))

        self.assertTrue(all(snapshots))
        self.assertEqual(
            tuple(
                entry.reading
                for entry in self.catalog.lookup("仮語")
            ),
            readings,
        )
        self.assertEqual(self.catalog.entry_count, len(readings))


if __name__ == "__main__":
    unittest.main()
