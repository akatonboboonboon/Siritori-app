from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from shiritori.room_persistence import (
    SNAPSHOT_SCHEMA_VERSION,
    deserialize_room_snapshot,
    serialize_room_snapshot,
)
from shiritori.rooms import (
    InMemoryRoomRepository,
    PlayerSeat,
    Role,
    RoomCoordinator,
    RoomMode,
    RoomSnapshot,
    RoomStatus,
    SeatController,
)


NOW = datetime(2026, 7, 25, 3, 0, tzinfo=timezone.utc)


def multiplayer_room(
    *,
    current_turn: int = 0,
    eliminated_seats: tuple[int, ...] = (),
    expected_kana: str | None = None,
    deadline_at: datetime | None = None,
) -> RoomSnapshot:
    return RoomSnapshot(
        room_id="four-player-room",
        mode=RoomMode.PVP,
        status=RoomStatus.ACTIVE,
        players=tuple(
            PlayerSeat(index, user_id, SeatController.HUMAN)
            for index, user_id in enumerate(
                ("alice", "bob", "carol", "dave")
            )
        ),
        current_turn=current_turn,
        eliminated_seats=eliminated_seats,
        expected_kana=expected_kana,
        turn_seconds=30,
        deadline_at=deadline_at,
    )


class MultiplayerEliminationTests(unittest.IsolatedAsyncioTestCase):
    async def test_players_are_eliminated_to_spectators_until_one_remains(
        self,
    ) -> None:
        repository = InMemoryRoomRepository([
            multiplayer_room(expected_kana="\u3042")
        ])
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)

        first = await coordinator.submit_user_turn(
            "four-player-room",
            "alice",
            surface="\u3042\u3093",
            reading="\u3042\u3093",
            canonical_key="\u3042\u3093",
            expected_version=0,
            operation_id="alice-loses",
            now=NOW,
        )
        assert first.snapshot is not None
        self.assertEqual(first.snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(first.snapshot.eliminated_seats, (0,))
        self.assertEqual(first.snapshot.active_seat_indexes, (1, 2, 3))
        self.assertEqual(first.snapshot.current_turn, 1)
        self.assertEqual(first.snapshot.expected_kana, "\u3042")
        self.assertEqual(first.snapshot.role_for_user("alice"), Role.SPECTATOR)

        second = await coordinator.submit_user_turn(
            "four-player-room",
            "bob",
            surface="\u3042\u304b\u3093",
            reading="\u3042\u304b\u3093",
            canonical_key="\u3042\u304b\u3093",
            expected_version=1,
            operation_id="bob-loses",
            now=NOW + timedelta(seconds=1),
        )
        assert second.snapshot is not None
        self.assertEqual(second.snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(second.snapshot.eliminated_seats, (0, 1))
        self.assertEqual(second.snapshot.expected_kana, "\u3042")
        self.assertEqual(second.snapshot.current_turn, 2)
        self.assertEqual(second.snapshot.role_for_user("bob"), Role.SPECTATOR)

        final = await coordinator.submit_user_turn(
            "four-player-room",
            "carol",
            surface="\u3042\u307e\u305e\u3093",
            reading="\u3042\u307e\u305e\u3093",
            canonical_key="\u3042\u307e\u305e\u3093",
            expected_version=2,
            operation_id="carol-loses",
            now=NOW + timedelta(seconds=2),
        )
        assert final.snapshot is not None
        self.assertEqual(final.snapshot.status, RoomStatus.FINISHED)
        self.assertEqual(final.snapshot.eliminated_seats, (0, 1, 2))
        self.assertEqual(final.snapshot.active_seat_indexes, (3,))
        self.assertEqual(final.snapshot.current_turn, 3)
        self.assertEqual(final.snapshot.losing_seat, 2)
        self.assertEqual(final.snapshot.end_reason, "ends_with_n")
        self.assertIsNone(final.snapshot.deadline_at)

    async def test_timeout_eliminates_only_current_player_and_resets_timer(
        self,
    ) -> None:
        due = NOW + timedelta(seconds=3)
        repository = InMemoryRoomRepository([
            multiplayer_room(
                expected_kana="\u304b",
                deadline_at=due,
            )
        ])
        coordinator = RoomCoordinator(repository, clock=lambda: due)

        outcome = await coordinator.expire_turn(
            "four-player-room",
            expected_version=0,
            operation_id="runtime:timeout-four-player",
            now=due,
        )

        assert outcome.snapshot is not None
        self.assertEqual(outcome.snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(outcome.snapshot.eliminated_seats, (0,))
        self.assertEqual(outcome.snapshot.current_turn, 1)
        self.assertEqual(outcome.snapshot.expected_kana, "\u304b")
        self.assertEqual(outcome.snapshot.timed_out_seat, 0)
        self.assertEqual(
            outcome.snapshot.deadline_at,
            due + timedelta(seconds=30),
        )

    async def test_regular_turn_skips_an_eliminated_seat_on_wraparound(
        self,
    ) -> None:
        snapshot = multiplayer_room(
            current_turn=3,
            eliminated_seats=(0,),
            expected_kana="\u3042",
        )
        coordinator = RoomCoordinator(
            InMemoryRoomRepository([snapshot]),
            clock=lambda: NOW,
        )

        outcome = await coordinator.submit_user_turn(
            "four-player-room",
            "dave",
            surface="\u3042\u304b",
            reading="\u3042\u304b",
            canonical_key="\u3042\u304b",
            expected_version=0,
            operation_id="skip-eliminated-seat",
            now=NOW,
        )

        assert outcome.snapshot is not None
        self.assertEqual(outcome.snapshot.current_turn, 1)
        self.assertEqual(outcome.snapshot.eliminated_seats, (0,))

    def test_eliminations_round_trip_and_legacy_snapshot_still_loads(
        self,
    ) -> None:
        snapshot = multiplayer_room(
            current_turn=1,
            eliminated_seats=(0,),
        )
        document = serialize_room_snapshot(snapshot)
        self.assertEqual(
            document["room_repository_schema"],
            SNAPSHOT_SCHEMA_VERSION,
        )
        self.assertEqual(deserialize_room_snapshot(document), snapshot)

        legacy = deepcopy(document)
        legacy["room_repository_schema"] = 2
        for field in (
            "eliminated_seats",
            "lives_per_player",
            "remaining_lives",
            "life_loss_events",
        ):
            legacy["snapshot"].pop(field)
        restored = deserialize_room_snapshot(legacy)
        self.assertEqual(restored.eliminated_seats, ())
        self.assertEqual(restored.current_turn, snapshot.current_turn)

    def test_active_room_rejects_only_one_surviving_seat(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least two active seats"):
            multiplayer_room(current_turn=3, eliminated_seats=(0, 1, 2))


if __name__ == "__main__":
    unittest.main()
