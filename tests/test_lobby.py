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
    LobbyNameConflict,
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
    normalize_room_name,
    room_name_key,
    validate_turn_seconds,
)
from shiritori.models import RoomRole, RoomStatus as StoredRoomStatus
from shiritori.rooms import (
    RoomStatus as ActiveRoomStatus,
    SeatController,
)


class InviteCodeTests(unittest.TestCase):
    def test_default_code_uses_unambiguous_secure_alphabet(self) -> None:
        code = generate_invite_code()
        self.assertEqual(len(code), 10)
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
        self.assertEqual(
            normalize_invite_code("abcd234567"), "ABCD234567")
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

    def test_room_name_normalization_has_one_display_and_identity_form(self) -> None:
        self.assertEqual(
            normalize_room_name(" \tＷｅｅｋｅｎｄ　ＭＡＴＣＨ\n"),
            "Weekend MATCH",
        )
        self.assertEqual(
            room_name_key(" \tＷｅｅｋｅｎｄ　ＭＡＴＣＨ\n"),
            room_name_key("weekend match"),
        )
        for invalid in ("", " \t\n", "x" * 65, 42):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_room_name(invalid)  # type: ignore[arg-type]


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

    def test_create_ignores_legacy_theme_and_persists_owner(self) -> None:
        room = self.create_room(max_players=4, theme_key="food")

        self.assertEqual(room.status, StoredRoomStatus.WAITING)
        self.assertEqual(room.room_code, "ABCD22")
        self.assertEqual(room.name, "Weekend match")
        self.assertEqual(room.max_players, 4)
        self.assertEqual(room.theme_key, "all")
        self.assertEqual(room.turn_seconds, 30)
        self.assertEqual(room.revision, 0)
        self.assertFalse(room.is_public)
        self.assertFalse(room.fill_empty_seats_with_bots)
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
            {"is_public": 1},
            {"fill_empty_seats_with_bots": 1},
            {"turn_seconds": 2},
            {"turn_seconds": True},
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.create_room(**overrides)

    def test_duplicate_name_uses_normalized_casefolded_identity(self) -> None:
        first = self.create_room(
            name=" \tＷｅｅｋｅｎｄ　ＭＡＴＣＨ\n",
            is_public=True,
            fill_empty_seats_with_bots=True,
        )

        self.assertEqual(first.name, "Weekend MATCH")
        self.assertTrue(first.is_public)
        self.assertTrue(first.fill_empty_seats_with_bots)
        with self.assertRaises(LobbyNameConflict):
            self.create_room(name="weekend match")
        other = self.create_room(name="Weekend matches")
        self.assertNotEqual(first.id, other.id)

    def test_active_room_keeps_name_reserved(self) -> None:
        room = self.create_room(fill_empty_seats_with_bots=True)
        self.service.set_ready("owner", room.room_code, ready=True)
        self.service.start("owner", room.room_code)

        with self.assertRaises(LobbyNameConflict):
            self.create_room(name="WEEKEND MATCH")

    def test_deleted_waiting_room_releases_name(self) -> None:
        room = self.create_room(name="Reusable")
        deleted = self.service.leave("owner", room.room_code)
        self.assertTrue(deleted.deleted)

        replacement = self.create_room(name="  Ｒｅｕｓａｂｌｅ  ")
        self.assertEqual(replacement.name, "Reusable")
        self.assertNotEqual(replacement.id, room.id)

    def test_public_waiting_listing_is_filtered_stable_and_limited(self) -> None:
        first = self.create_room(name="First", is_public=True)
        self.create_room(name="Private", is_public=False)
        active = self.create_room(
            name="Active",
            is_public=True,
            fill_empty_seats_with_bots=True,
        )
        self.service.set_ready("owner", active.room_code, ready=True)
        self.service.start("owner", active.room_code)
        deleted = self.create_room(name="Deleted", is_public=True)
        self.service.leave("owner", deleted.room_code)
        last = self.create_room(name="Last", is_public=True)

        self.assertEqual(
            tuple(room.id for room in self.service.list_public_waiting()),
            (first.id, last.id),
        )
        self.assertEqual(
            tuple(room.id for room in self.service.list_public_rooms()),
            (first.id, active.id, last.id),
        )
        self.assertEqual(
            tuple(
                room.id
                for room in self.service.list_public_rooms(limit=2)
            ),
            (first.id, active.id),
        )
        self.assertEqual(
            self.service.list_public_waiting(limit=1),
            (first,),
        )
        for invalid in (0, 101, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.service.list_public_waiting(limit=invalid)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    self.repository.list_public_waiting(limit=invalid)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    self.service.list_public_rooms(limit=invalid)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    self.repository.list_public_rooms(limit=invalid)  # type: ignore[arg-type]

    def test_obsolete_theme_key_format_is_ignored(self) -> None:
        room = self.create_room(theme_key="../food")
        self.assertEqual(room.theme_key, "all")

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

        closed_room = self.create_room(
            name="No spectators",
            allow_spectators=False,
        )
        with self.assertRaises(SpectatorsDisabledError):
            self.service.join_as_spectator("other-watcher", closed_room.room_code)

    def test_active_public_spectator_joins_are_atomic_and_idempotent(
        self,
    ) -> None:
        room = self.create_room(
            is_public=True,
            fill_empty_seats_with_bots=True,
        )
        self.service.set_ready("owner", room.room_code, ready=True)
        started = self.service.start("owner", room.room_code)

        def join(user_id: str) -> str:
            joined = self.service.join_as_spectator(
                user_id,
                room.room_code,
            )
            return joined.member_for(user_id).role.value  # type: ignore[union-attr]

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                executor.map(join, ("watcher-a", "watcher-b"))
            )

        self.assertEqual(outcomes, ("spectator", "spectator"))
        current = self.service.get_room(room.room_code)
        self.assertEqual(
            tuple(member.user_id for member in current.spectators),
            ("watcher-a", "watcher-b"),
        )
        self.assertEqual(current.revision, started.lobby.revision + 2)
        for user_id in ("watcher-a", "watcher-b"):
            self.assertEqual(
                self.service.active_game_id(user_id, room.room_code),
                started.game_id,
            )

        same = self.service.join_as_spectator(
            "watcher-a",
            room.room_code,
        )
        self.assertEqual(same.revision, current.revision)
        persisted_start = self.repository._starts_by_game_id[started.game_id]
        self.assertEqual(
            persisted_start.active_room.spectators,
            ("watcher-a", "watcher-b"),
        )
        self.assertEqual(persisted_start.active_room.state_version, 2)
        with self.assertRaises(LobbyStateError):
            self.service.join_as_player("late-player", room.room_code)

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

    def test_owner_updates_waiting_settings_and_resets_all_readiness(
        self,
    ) -> None:
        room = self.create_room(max_players=4)
        self.service.join_as_player("guest", room.room_code)
        self.service.set_ready("owner", room.room_code, ready=True)
        ready = self.service.set_ready(
            "guest",
            room.room_code,
            ready=True,
        )

        updated = self.service.update_settings(
            "owner",
            room.room_code,
            expected_revision=ready.revision,
            max_players=3,
            allow_spectators=False,
            turn_seconds=None,
            is_public=True,
            fill_empty_seats_with_bots=True,
        )

        self.assertEqual(updated.revision, ready.revision + 1)
        self.assertEqual(updated.max_players, 3)
        self.assertFalse(updated.allow_spectators)
        self.assertIsNone(updated.turn_seconds)
        self.assertTrue(updated.is_public)
        self.assertTrue(updated.fill_empty_seats_with_bots)
        self.assertTrue(all(not player.ready for player in updated.players))
        self.assertEqual(
            tuple(player.user_id for player in updated.players),
            ("owner", "guest"),
        )
        with self.assertRaises(LobbyNotReadyError):
            self.service.start("owner", room.room_code)

    def test_ready_rejects_gameplay_settings_not_seen_by_player(
        self,
    ) -> None:
        room = self.create_room(max_players=4)
        joined = self.service.join_as_player("guest", room.room_code)
        displayed_settings = (
            joined.max_players,
            joined.turn_seconds,
            joined.fill_empty_seats_with_bots,
        )
        updated = self.service.update_settings(
            "owner",
            room.room_code,
            expected_revision=joined.revision,
            max_players=3,
            allow_spectators=joined.allow_spectators,
            turn_seconds=None,
            is_public=joined.is_public,
            fill_empty_seats_with_bots=True,
        )

        with self.assertRaises(LobbyRevisionConflict) as service_conflict:
            self.service.set_ready(
                "guest",
                room.room_code,
                ready=True,
                expected_gameplay_settings=displayed_settings,
            )
        self.assertEqual(service_conflict.exception.current_room, updated)
        with self.assertRaises(LobbyRevisionConflict) as repo_conflict:
            self.repository.set_ready(
                room_id=room.id,
                user_id="guest",
                ready=True,
                expected_gameplay_settings=displayed_settings,
            )
        self.assertEqual(repo_conflict.exception.current_room, updated)

        confirmed = self.service.set_ready(
            "guest",
            room.room_code,
            ready=True,
            expected_gameplay_settings=(
                updated.max_players,
                updated.turn_seconds,
                updated.fill_empty_seats_with_bots,
            ),
        )
        confirmed_guest = confirmed.member_for("guest")
        self.assertIsNotNone(confirmed_guest)
        self.assertTrue(confirmed_guest.ready)

    def test_unchanged_settings_are_a_read_only_success(self) -> None:
        room = self.create_room(max_players=4)
        ready = self.service.set_ready(
            "owner",
            room.room_code,
            ready=True,
        )

        unchanged = self.service.update_settings(
            "owner",
            room.room_code,
            expected_revision=ready.revision,
            max_players=ready.max_players,
            allow_spectators=ready.allow_spectators,
            turn_seconds=ready.turn_seconds,
            is_public=ready.is_public,
            fill_empty_seats_with_bots=ready.fill_empty_seats_with_bots,
        )

        self.assertEqual(unchanged, ready)
        self.assertTrue(unchanged.member_for("owner").ready)  # type: ignore[union-attr]

    def test_waiting_settings_require_owner_and_current_revision(self) -> None:
        room = self.create_room(max_players=4)
        joined = self.service.join_as_player("guest", room.room_code)
        self.service.join_as_spectator("watcher", room.room_code)

        for user_id in ("guest", "watcher", "stranger"):
            with self.subTest(user_id=user_id), self.assertRaises(
                LobbyAuthorizationError
            ):
                self.service.update_settings(
                    user_id,
                    room.room_code,
                    expected_revision=joined.revision,
                    max_players=3,
                    allow_spectators=True,
                    turn_seconds=10,
                    is_public=True,
                    fill_empty_seats_with_bots=False,
                )

        current = self.service.get_room(room.room_code)
        with self.assertRaises(LobbyRevisionConflict) as caught:
            self.service.update_settings(
                "owner",
                room.room_code,
                expected_revision=room.revision,
                max_players=3,
                allow_spectators=True,
                turn_seconds=10,
                is_public=True,
                fill_empty_seats_with_bots=False,
            )
        self.assertEqual(caught.exception.current_room, current)
        self.assertEqual(self.service.get_room(room.room_code), current)

    def test_waiting_settings_cannot_shrink_below_present_players(self) -> None:
        room = self.create_room(max_players=4)
        self.service.join_as_player("guest-a", room.room_code)
        current = self.service.join_as_player("guest-b", room.room_code)

        with self.assertRaises(LobbyCapacityError):
            self.service.update_settings(
                "owner",
                room.room_code,
                expected_revision=current.revision,
                max_players=2,
                allow_spectators=False,
                turn_seconds=None,
                is_public=True,
                fill_empty_seats_with_bots=True,
            )

        self.assertEqual(self.service.get_room(room.room_code), current)

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

    def test_fill_empty_seats_starts_one_human_with_permanent_bots(self) -> None:
        room = self.create_room(
            max_players=4,
            fill_empty_seats_with_bots=True,
        )
        with self.assertRaises(LobbyNotReadyError):
            self.service.start("owner", room.room_code)

        self.service.set_ready("owner", room.room_code, ready=True)
        result = self.service.start("owner", room.room_code)

        self.assertEqual(len(result.active_room.players), 4)
        self.assertEqual(
            tuple(seat.owner_user_id for seat in result.active_room.players),
            ("owner", None, None, None),
        )
        self.assertEqual(
            tuple(seat.controller for seat in result.active_room.players),
            (
                SeatController.HUMAN,
                SeatController.BOT,
                SeatController.BOT,
                SeatController.BOT,
            ),
        )
        self.assertTrue(
            all(
                not seat.handback_pending
                for seat in result.active_room.players[1:]
            )
        )
        self.assertEqual(result.active_room.bot_difficulty, "normal")

    def test_fill_empty_seats_requires_every_present_human_ready(self) -> None:
        room = self.create_room(
            max_players=4,
            fill_empty_seats_with_bots=True,
        )
        self.service.join_as_player("guest", room.room_code)
        self.service.set_ready("owner", room.room_code, ready=True)
        with self.assertRaises(LobbyNotReadyError):
            self.service.start("owner", room.room_code)

        self.service.set_ready("guest", room.room_code, ready=True)
        result = self.service.start("owner", room.room_code)
        self.assertEqual(
            tuple(seat.owner_user_id for seat in result.active_room.players),
            ("owner", "guest", None, None),
        )

    def test_fill_empty_seats_adds_no_bot_when_room_is_full(self) -> None:
        room = self.create_room(
            max_players=2,
            fill_empty_seats_with_bots=True,
        )
        self.service.join_as_player("guest", room.room_code)
        self.service.set_ready("owner", room.room_code, ready=True)
        self.service.set_ready("guest", room.room_code, ready=True)

        result = self.service.start("owner", room.room_code)

        self.assertEqual(
            tuple(seat.owner_user_id for seat in result.active_room.players),
            ("owner", "guest"),
        )
        self.assertTrue(
            all(
                seat.controller is SeatController.HUMAN
                for seat in result.active_room.players
            )
        )

    def test_repository_rejects_corrupt_permanent_bot_layout(self) -> None:
        class CorruptingRepository(InMemoryLobbyRepository):
            def activate_waiting(self, **kwargs):  # type: ignore[no-untyped-def]
                active_room = kwargs["active_room"]
                seats = active_room.players
                kwargs["active_room"] = replace(
                    active_room,
                    players=(
                        seats[0],
                        replace(
                            seats[1],
                            owner_user_id="impostor",
                            controller=SeatController.HUMAN,
                        ),
                        *seats[2:],
                    ),
                )
                return super().activate_waiting(**kwargs)

        repository = CorruptingRepository()
        service = LobbyService(
            repository,
            code_factory=lambda: "BOTS22",
            game_id_factory=lambda: "bot-layout-game",
            seat_picker=lambda _: 0,
        )
        room = service.create_pvp_room(
            "owner",
            name="Bot layout",
            max_players=3,
            fill_empty_seats_with_bots=True,
        )
        service.set_ready("owner", room.room_code, ready=True)

        with self.assertRaises(LobbyRevisionConflict):
            service.start("owner", room.room_code)

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
        self.assertEqual(room.theme_key, "all")
        self.assertEqual(result.active_room.theme_key, "all")
        self.assertEqual(result.active_room.bot_difficulty, "normal")
        self.assertIsNotNone(result.active_room.deadline_at)
        self.assertEqual(result.active_room.spectators, ("watcher",))
        self.assertEqual(
            tuple(seat.owner_user_id for seat in result.active_room.players),
            ("owner", "guest"),
        )
        self.assertEqual(self.service.list_public_rooms(), ())
        joined = self.service.join_as_spectator(
            "late",
            room.room_code,
        )
        self.assertEqual(
            tuple(member.user_id for member in joined.spectators),
            ("watcher", "late"),
        )
        self.assertEqual(
            self.service.active_game_id("late", room.room_code),
            result.game_id,
        )

    def test_finished_round_returns_to_waiting_and_starts_new_game(self) -> None:
        repository = InMemoryLobbyRepository()
        game_ids = iter(("round-0001", "round-0002"))
        service = LobbyService(
            repository,
            code_factory=lambda: "ROUND22",
            game_id_factory=lambda: next(game_ids),
            seat_picker=lambda _: 0,
        )
        room = service.create_pvp_room(
            "owner",
            name="Rematch room",
            max_players=2,
            turn_seconds=30,
        )
        service.join_as_player("guest", room.room_code)
        service.join_as_spectator("watcher", room.room_code)
        service.set_ready("owner", room.room_code, ready=True)
        service.set_ready("guest", room.room_code, ready=True)
        first = service.start("owner", room.room_code)

        with self.assertRaises(LobbyStateError):
            service.return_to_waiting("owner", first.game_id)

        finished = replace(
            first.active_room,
            status=ActiveRoomStatus.FINISHED,
            deadline_at=None,
            end_reason="no_legal_move",
            state_version=first.active_room.state_version + 1,
        )
        repository.record_finished_game(finished)

        with self.assertRaises(LobbyAuthorizationError):
            service.return_to_waiting("outsider", first.game_id)
        waiting = service.return_to_waiting("watcher", first.game_id)
        self.assertEqual(waiting.status, StoredRoomStatus.WAITING)
        self.assertEqual(waiting.room_code, room.room_code)
        self.assertEqual(waiting.revision, first.lobby.revision + 1)
        self.assertEqual(
            tuple(member.user_id for member in waiting.members),
            ("owner", "guest", "watcher"),
        )
        self.assertTrue(
            all(not player.ready for player in waiting.players)
        )
        self.assertEqual(
            tuple(member.user_id for member in waiting.spectators),
            ("watcher",),
        )

        # An exact transport retry is a read-only success.
        self.assertEqual(
            service.return_to_waiting("owner", first.game_id),
            waiting,
        )
        with self.assertRaises(LobbyNotReadyError):
            service.start("owner", room.room_code)

        service.set_ready("owner", room.room_code, ready=True)
        service.set_ready("guest", room.room_code, ready=True)
        second = service.start("owner", room.room_code)
        self.assertEqual(second.game_id, "round-0002")
        self.assertNotEqual(second.game_id, first.game_id)
        self.assertEqual(second.active_room.history, ())
        self.assertEqual(second.active_room.expected_kana, None)
        self.assertEqual(
            service.open_room_for_game("watcher", first.game_id),
            second.lobby,
        )
        with self.assertRaises(LobbyRoomNotFound):
            service.open_room_for_game("outsider", first.game_id)
        with self.assertRaises(LobbyRoomNotFound):
            service.open_room_for_game("watcher", "missing-game")
        with self.assertRaises(LobbyStateError):
            service.return_to_waiting("owner", first.game_id)

    def test_active_room_with_spectators_disabled_is_not_joinable(
        self,
    ) -> None:
        room = self.create_room(
            is_public=True,
            allow_spectators=False,
            fill_empty_seats_with_bots=True,
        )
        self.service.set_ready("owner", room.room_code, ready=True)
        self.service.start("owner", room.room_code)

        self.assertEqual(self.service.list_public_rooms(), ())
        with self.assertRaises(SpectatorsDisabledError):
            self.service.join_as_spectator("late", room.room_code)
        with self.assertRaises(SpectatorsDisabledError):
            self.repository.join_active_spectator(
                room_id=room.id,
                user_id="late",
            )

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
