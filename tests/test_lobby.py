from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest

from shiritori.lobby import (
    INVITE_CODE_ALPHABET,
    InMemoryLobbyRepository,
    InviteCodeGenerationError,
    LobbyAuthorizationError,
    LobbyCapacityError,
    LobbyMemberError,
    LobbyNotReadyError,
    LobbyRepository,
    LobbyRevisionConflict,
    LobbyRoomNotFound,
    LobbyService,
    LobbyStateError,
    SpectatorsDisabledError,
    generate_invite_code,
    normalize_invite_code,
    validate_turn_seconds,
)
from shiritori.models import RoomRole, RoomStatus as StoredRoomStatus
from shiritori.rooms import RoomStatus as ActiveRoomStatus


class InviteCodeTests(unittest.TestCase):
    def test_default_code_uses_unambiguous_secure_alphabet(self) -> None:
        code = generate_invite_code()
        self.assertEqual(len(code), 6)
        self.assertTrue(set(code).issubset(set(INVITE_CODE_ALPHABET)))
        self.assertTrue({"0", "1", "I", "O"}.isdisjoint(code))

    def test_custom_chooser_is_validated(self) -> None:
        self.assertEqual(
            generate_invite_code(4, chooser=lambda _: "A"),
            "AAAA",
        )
        with self.assertRaises(ValueError):
            generate_invite_code(4, chooser=lambda _: "!")
        with self.assertRaises(ValueError):
            generate_invite_code(3)

    def test_normalization_is_case_insensitive_but_never_fuzzy(self) -> None:
        self.assertEqual(normalize_invite_code("  abcd23 "), "ABCD23")
        for invalid in ("ABC", "ABCD-23", "ABCD 23", "ABCD23extraextra", "ＡＢＣＤ"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_invite_code(invalid)

    def test_turn_time_accepts_unlimited_and_inclusive_bounds(self) -> None:
        self.assertIsNone(validate_turn_seconds(None))
        self.assertEqual(validate_turn_seconds(3), 3)
        self.assertEqual(validate_turn_seconds(180), 180)
        for invalid in (2, 181, True, 3.0):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_turn_seconds(invalid)  # type: ignore[arg-type]


class LobbyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryLobbyRepository()
        self.codes = iter(
            (
                "ABCD22",
                "ABCD23",
                "ABCD24",
                "ABCD25",
                "ABCD26",
                "ABCD27",
                "ABCD28",
            )
        )
        self.service = LobbyService(
            self.repository,
            code_factory=lambda: next(self.codes),
            game_id_factory=lambda: "game-0001",
            seat_picker=lambda count: count - 1,
        )

    def create_room(self, **overrides: object):
        values: dict[str, object] = {
            "name": "Weekend match",
            "max_players": 2,
            "allow_spectators": True,
            "theme_key": "all",
            "turn_seconds": 30,
        }
        values.update(overrides)
        return self.service.create_pvp_room("owner", **values)  # type: ignore[arg-type]

    def test_create_waiting_room_persists_settings_and_owner(self) -> None:
        room = self.create_room(max_players=4, theme_key="food")

        self.assertEqual(room.status, StoredRoomStatus.WAITING)
        self.assertEqual(room.room_code, "ABCD22")
        self.assertEqual(room.name, "Weekend match")
        self.assertEqual(room.max_players, 4)
        self.assertEqual(room.theme_key, "food")
        self.assertEqual(room.turn_seconds, 30)
        self.assertEqual(room.revision, 0)
        self.assertEqual(
            room.players,
            (room.member_for("owner"),),
        )
        self.assertEqual(room.players[0].seat_index, 0)
        self.assertFalse(room.players[0].ready)
        self.assertIsInstance(self.repository, LobbyRepository)

    def test_create_retries_unique_constraint_conflict(self) -> None:
        first = self.create_room()
        retry_codes = iter((first.room_code, "GHJK22"))
        retry_service = LobbyService(
            self.repository,
            code_factory=lambda: next(retry_codes),
            max_code_attempts=2,
        )

        second = retry_service.create_pvp_room("another", name="Second")

        self.assertEqual(second.room_code, "GHJK22")
        self.assertNotEqual(first.id, second.id)

    def test_create_has_bounded_collision_retries(self) -> None:
        first = self.create_room()
        service = LobbyService(
            self.repository,
            code_factory=lambda: first.room_code,
            max_code_attempts=2,
        )
        with self.assertRaises(InviteCodeGenerationError):
            service.create_pvp_room("another", name="Never created")

    def test_create_rejects_invalid_settings_before_persistence(self) -> None:
        invalid_cases = (
            {"name": " "},
            {"max_players": 1},
            {"max_players": True},
            {"allow_spectators": 1},
            {"theme_key": "../food"},
            {"theme_key": "1food"},
            {"theme_key": "a" * 33},
            {"turn_seconds": 2},
            {"turn_seconds": True},
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.create_room(**overrides)

    def test_theme_key_matches_theme_definition_identifier_rule(self) -> None:
        room = self.create_room(theme_key="a" + "1" * 31)
        self.assertEqual(room.theme_key, "a" + "1" * 31)

    def test_get_by_code_is_exact_and_never_crosses_rooms(self) -> None:
        first = self.create_room(name="First")
        second = self.create_room(name="Second")

        self.assertEqual(self.service.get_room("abcd22").id, first.id)
        self.assertEqual(self.service.get_room("ABCD23").id, second.id)
        with self.assertRaises(ValueError):
            self.service.get_room("ABC")
        with self.assertRaises(LobbyRoomNotFound):
            self.service.get_room("ABCD")
        with self.assertRaises(LobbyRoomNotFound):
            self.service.get_room("ABCD24")

    def test_join_players_allocates_seats_and_enforces_capacity(self) -> None:
        room = self.create_room(max_players=2)
        joined = self.service.join_as_player("guest", room.room_code)

        self.assertEqual(
            tuple((member.user_id, member.seat_index) for member in joined.players),
            (("owner", 0), ("guest", 1)),
        )
        self.assertEqual(joined.revision, 1)

        idempotent = self.service.join_as_player("guest", room.room_code)
        self.assertEqual(idempotent.revision, 1)
        with self.assertRaises(LobbyCapacityError):
            self.service.join_as_player("third", room.room_code)

    def test_atomic_join_allows_only_one_contender_for_last_seat(self) -> None:
        room = self.create_room(max_players=2)

        def join(user_id: str) -> str:
            try:
                self.service.join_as_player(user_id, room.room_code)
            except LobbyCapacityError:
                return "full"
            return "joined"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(join, ("guest-a", "guest-b")))

        self.assertEqual(sorted(outcomes), ["full", "joined"])
        self.assertEqual(len(self.service.get_room(room.room_code).players), 2)

    def test_spectator_permission_and_role_changes(self) -> None:
        open_room = self.create_room()
        watched = self.service.join_as_spectator(
            "watcher",
            open_room.room_code,
        )
        self.assertEqual(watched.spectators[0].user_id, "watcher")
        self.assertIsNone(watched.spectators[0].seat_index)
        with self.assertRaises(LobbyMemberError):
            self.service.join_as_player("watcher", open_room.room_code)

        closed_room = self.create_room(allow_spectators=False)
        with self.assertRaises(SpectatorsDisabledError):
            self.service.join_as_spectator("other-watcher", closed_room.room_code)

    def test_only_players_can_change_readiness(self) -> None:
        room = self.create_room()
        self.service.join_as_player("guest", room.room_code)
        self.service.join_as_spectator("watcher", room.room_code)

        ready = self.service.set_ready("owner", room.room_code, ready=True)
        self.assertTrue(ready.member_for("owner").ready)  # type: ignore[union-attr]
        same = self.service.set_ready("owner", room.room_code, ready=True)
        self.assertEqual(same.revision, ready.revision)

        with self.assertRaises(LobbyAuthorizationError):
            self.service.set_ready("watcher", room.room_code, ready=True)
        with self.assertRaises(LobbyMemberError):
            self.service.set_ready("stranger", room.room_code, ready=True)

    def test_start_requires_owner_two_players_and_every_player_ready(self) -> None:
        room = self.create_room()
        with self.assertRaises(LobbyNotReadyError):
            self.service.start("owner", room.room_code)

        self.service.join_as_player("guest", room.room_code)
        self.service.set_ready("owner", room.room_code, ready=True)
        with self.assertRaises(LobbyAuthorizationError):
            self.service.start("guest", room.room_code)
        with self.assertRaises(LobbyNotReadyError):
            self.service.start("owner", room.room_code)

    def test_start_persists_random_turn_free_first_word_and_spectators(self) -> None:
        room = self.create_room(theme_key="country", turn_seconds=3)
        self.service.join_as_player("guest", room.room_code)
        self.service.join_as_spectator("watcher", room.room_code)
        self.service.set_ready("owner", room.room_code, ready=True)
        self.service.set_ready("guest", room.room_code, ready=True)

        result = self.service.start("owner", room.room_code)

        self.assertEqual(result.lobby.status, StoredRoomStatus.ACTIVE)
        self.assertEqual(result.game_id, "game-0001")
        self.assertEqual(result.active_room.room_id, "game-0001")
        self.assertEqual(result.active_room.status, ActiveRoomStatus.ACTIVE)
        self.assertEqual(result.active_room.current_turn, 1)
        self.assertEqual(result.active_room.expected_kana, None)
        self.assertEqual(result.active_room.history, ())
        self.assertEqual(result.active_room.turn_seconds, 3)
        self.assertEqual(result.active_room.theme_key, "country")
        self.assertEqual(result.active_room.bot_difficulty, "normal")
        self.assertIsNotNone(result.active_room.deadline_at)
        self.assertEqual(result.active_room.spectators, ("watcher",))
        self.assertEqual(
            tuple(seat.owner_user_id for seat in result.active_room.players),
            ("owner", "guest"),
        )
        with self.assertRaises(LobbyStateError):
            self.service.join_as_spectator("late", room.room_code)

    def test_active_game_lookup_is_member_only_and_requires_started_room(self) -> None:
        room = self.create_room()
        with self.assertRaises(LobbyRoomNotFound):
            self.service.active_game_id("owner", room.room_code)

        self.service.join_as_player("guest", room.room_code)
        self.service.join_as_spectator("watcher", room.room_code)
        self.service.set_ready("owner", room.room_code, ready=True)
        self.service.set_ready("guest", room.room_code, ready=True)
        started = self.service.start("owner", room.room_code)

        for user_id in ("owner", "guest", "watcher"):
            with self.subTest(user_id=user_id):
                self.assertEqual(
                    self.service.active_game_id(user_id, room.room_code.lower()),
                    started.game_id,
                )
        with self.assertRaises(LobbyRoomNotFound):
            self.service.active_game_id("outsider", room.room_code)

    def test_repository_rechecks_revision_at_start_boundary(self) -> None:
        class RacingRepository(InMemoryLobbyRepository):
            def activate_waiting(self, **kwargs):  # type: ignore[no-untyped-def]
                self.join_waiting(
                    room_id=kwargs["room_id"],
                    user_id="racer",
                    role=RoomRole.SPECTATOR,
                )
                return super().activate_waiting(**kwargs)

        repository = RacingRepository()
        service = LobbyService(
            repository,
            code_factory=lambda: "RACE22",
            game_id_factory=lambda: "race-game",
            seat_picker=lambda _: 0,
        )
        room = service.create_pvp_room("owner", name="Race", max_players=3)
        service.join_as_player("guest", room.room_code)
        service.set_ready("owner", room.room_code, ready=True)
        service.set_ready("guest", room.room_code, ready=True)

        with self.assertRaises(LobbyRevisionConflict) as caught:
            service.start("owner", room.room_code)
        current_room = caught.exception.current_room
        self.assertIsNotNone(current_room)
        racer = current_room.member_for("racer")  # type: ignore[union-attr]
        self.assertIsNotNone(racer)
        self.assertEqual(racer.role, RoomRole.SPECTATOR)  # type: ignore[union-attr]

    def test_leave_spectator_owner_transfer_reseat_and_last_player_delete(self) -> None:
        room = self.create_room(max_players=3)
        self.service.join_as_player("guest-a", room.room_code)
        self.service.join_as_player("guest-b", room.room_code)
        self.service.join_as_spectator("watcher", room.room_code)

        without_watcher = self.service.leave("watcher", room.room_code)
        self.assertFalse(without_watcher.deleted)
        self.assertIsNotNone(without_watcher.room)
        self.assertEqual(
            without_watcher.room.spectators,  # type: ignore[union-attr]
            (),
        )

        transferred = self.service.leave("owner", room.room_code)
        self.assertFalse(transferred.deleted)
        self.assertIsNotNone(transferred.room)
        self.assertEqual(
            transferred.room.owner_user_id,  # type: ignore[union-attr]
            "guest-a",
        )
        self.assertEqual(
            tuple(
                (member.user_id, member.seat_index)
                for member in transferred.room.players  # type: ignore[union-attr]
            ),
            (("guest-a", 0), ("guest-b", 1)),
        )

        self.service.leave("guest-a", room.room_code)
        deleted = self.service.leave("guest-b", room.room_code)
        self.assertTrue(deleted.deleted)
        self.assertIsNone(deleted.room)
        with self.assertRaises(LobbyRoomNotFound):
            self.service.get_room(room.room_code)

    def test_leave_after_start_is_rejected(self) -> None:
        room = self.create_room()
        self.service.join_as_player("guest", room.room_code)
        self.service.set_ready("owner", room.room_code, ready=True)
        self.service.set_ready("guest", room.room_code, ready=True)
        self.service.start("owner", room.room_code)

        with self.assertRaises(LobbyStateError):
            self.service.leave("owner", room.room_code)

    def test_repository_cannot_return_a_different_code(self) -> None:
        room = self.create_room()

        class ConfusedRepository:
            def get_by_code(self, room_code: str):
                return replace(room, room_code="WRNG22")

        confused = LobbyService(ConfusedRepository())  # type: ignore[arg-type]
        with self.assertRaises(RuntimeError):
            confused.get_room(room.room_code)


if __name__ == "__main__":
    unittest.main()
