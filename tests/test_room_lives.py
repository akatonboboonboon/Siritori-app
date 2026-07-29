"""Regression coverage for multiplayer lives and visible loss reasons."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from shiritori.room_persistence import (
    SNAPSHOT_SCHEMA_VERSION,
    RoomSnapshotCorruptError,
    deserialize_room_snapshot,
    serialize_room_snapshot,
)
from shiritori.rooms import (
    InMemoryRoomRepository,
    LifeLossRecord,
    RoomCoordinator,
    RoomMode,
    RoomStatus,
    SeatController,
    TurnRecord,
    create_room_snapshot,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def pvp_with_lives(
    *,
    room_id: str,
    lives: int = 3,
    expected_kana: str | None = "り",
    player_count: int = 2,
):
    users = tuple(("alice", "bob", "carol")[:player_count])
    snapshot = create_room_snapshot(
        room_id,
        users,
        mode=RoomMode.PVP,
        lives_per_player=lives,
        now=NOW,
        seat_picker=lambda _count: 0,
    )
    return replace(snapshot, expected_kana=expected_kana)


class LifeSystemTests(unittest.IsolatedAsyncioTestCase):
    async def test_ends_with_n_loses_one_life_and_exposes_word_reason(
        self,
    ) -> None:
        room = pvp_with_lives(room_id="ends-with-n")
        coordinator = RoomCoordinator(InMemoryRoomRepository([room]))

        first = await coordinator.submit_user_turn(
            room.room_id,
            "alice",
            surface="りん",
            reading="りん",
            canonical_key="りん",
            expected_version=0,
            operation_id="ends-with-n-once",
            now=NOW,
        )
        retried = await coordinator.submit_user_turn(
            room.room_id,
            "alice",
            surface="りん",
            reading="りん",
            canonical_key="りん",
            expected_version=0,
            operation_id="ends-with-n-once",
            now=NOW + timedelta(seconds=1),
        )

        assert first.snapshot is not None
        snapshot = first.snapshot
        self.assertEqual(snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(snapshot.remaining_lives, (2, 3))
        self.assertEqual(snapshot.eliminated_seats, ())
        self.assertEqual(snapshot.current_turn, 1)
        self.assertEqual(snapshot.expected_kana, "り")
        self.assertEqual(tuple(turn.surface for turn in snapshot.history), ("りん",))
        self.assertEqual(len(snapshot.life_loss_events), 1)
        event = snapshot.life_loss_events[-1]
        self.assertEqual(event.seat_index, 0)
        self.assertEqual(event.reason, "ends_with_n")
        self.assertEqual(event.surface, "りん")
        self.assertEqual(event.reading, "りん")
        self.assertEqual(event.remaining_lives, 2)
        self.assertFalse(event.eliminated)
        self.assertEqual(event.occurred_at, NOW)
        self.assertTrue(retried.duplicate)
        self.assertEqual(retried.snapshot, snapshot)

    async def test_duplicate_loses_life_without_history_and_keeps_attempt(
        self,
    ) -> None:
        room = pvp_with_lives(room_id="duplicate")
        accepted = TurnRecord(
            surface="りす",
            reading="りす",
            canonical_key="りす",
            seat_index=1,
            actor_user_id="bob",
            by_bot=False,
            submitted_at=NOW - timedelta(seconds=1),
        )
        room = replace(room, history=(accepted,))
        coordinator = RoomCoordinator(InMemoryRoomRepository([room]))

        outcome = await coordinator.submit_user_turn(
            room.room_id,
            "alice",
            surface="リス",
            reading="りす",
            canonical_key="りす",
            expected_version=0,
            operation_id="duplicate-once",
            now=NOW,
        )

        assert outcome.snapshot is not None
        snapshot = outcome.snapshot
        self.assertEqual(snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(snapshot.remaining_lives, (2, 3))
        self.assertEqual(snapshot.current_turn, 1)
        self.assertEqual(snapshot.expected_kana, "り")
        self.assertEqual(snapshot.history, (accepted,))
        event = snapshot.life_loss_events[-1]
        self.assertEqual(event.reason, "duplicate")
        self.assertEqual(event.surface, "リス")
        self.assertEqual(event.reading, "りす")
        self.assertEqual(event.remaining_lives, 2)
        self.assertFalse(event.eliminated)

    async def test_last_life_eliminates_and_finishes_two_player_match(
        self,
    ) -> None:
        room = pvp_with_lives(room_id="last-life")
        room = replace(room, remaining_lives=(1, 3))
        coordinator = RoomCoordinator(InMemoryRoomRepository([room]))

        outcome = await coordinator.submit_user_turn(
            room.room_id,
            "alice",
            surface="りん",
            reading="りん",
            canonical_key="りん",
            expected_version=0,
            operation_id="last-life-loss",
            now=NOW,
        )

        assert outcome.snapshot is not None
        snapshot = outcome.snapshot
        self.assertEqual(snapshot.status, RoomStatus.FINISHED)
        self.assertEqual(snapshot.remaining_lives, (0, 3))
        self.assertEqual(snapshot.eliminated_seats, (0,))
        self.assertEqual(snapshot.current_turn, 1)
        self.assertIsNone(snapshot.expected_kana)
        event = snapshot.life_loss_events[-1]
        self.assertEqual(event.remaining_lives, 0)
        self.assertTrue(event.eliminated)

    async def test_surrender_zeroes_all_remaining_lives_immediately(
        self,
    ) -> None:
        room = pvp_with_lives(
            room_id="surrender-five",
            lives=5,
            player_count=3,
        )
        coordinator = RoomCoordinator(InMemoryRoomRepository([room]))

        outcome = await coordinator.surrender(
            room.room_id,
            "alice",
            expected_version=0,
            operation_id="surrender-five",
            now=NOW,
        )

        assert outcome.snapshot is not None
        snapshot = outcome.snapshot
        self.assertEqual(snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(snapshot.remaining_lives, (0, 5, 5))
        self.assertEqual(snapshot.eliminated_seats, (0,))
        self.assertEqual(snapshot.current_turn, 1)
        event = snapshot.life_loss_events[-1]
        self.assertEqual(event.reason, "surrender")
        self.assertEqual(event.seat_index, 0)
        self.assertEqual(event.remaining_lives, 0)
        self.assertTrue(event.eliminated)
        self.assertIsNone(event.surface)
        self.assertIsNone(event.reading)

    async def test_timeout_loses_one_life_without_elimination(
        self,
    ) -> None:
        room = pvp_with_lives(room_id="timeout")
        room = replace(
            room,
            players=(
                room.players[0],
                replace(
                    room.players[1],
                    controller=SeatController.BOT,
                    handback_pending=True,
                ),
            ),
            turn_seconds=30,
            deadline_at=NOW,
        )
        coordinator = RoomCoordinator(InMemoryRoomRepository([room]))

        outcome = await coordinator.expire_turn(
            room.room_id,
            expected_version=0,
            operation_id="runtime:timeout-life",
            now=NOW,
        )

        assert outcome.snapshot is not None
        snapshot = outcome.snapshot
        self.assertEqual(snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(snapshot.remaining_lives, (2, 3))
        self.assertEqual(snapshot.eliminated_seats, ())
        self.assertEqual(snapshot.current_turn, 1)
        self.assertEqual(snapshot.timed_out_seat, 0)
        self.assertEqual(snapshot.deadline_at, NOW + timedelta(seconds=30))
        self.assertEqual(snapshot.players[1].controller, SeatController.HUMAN)
        self.assertFalse(snapshot.players[1].handback_pending)
        event = snapshot.life_loss_events[-1]
        self.assertEqual(event.reason, "timeout")
        self.assertEqual(event.remaining_lives, 2)
        self.assertFalse(event.eliminated)
        self.assertIsNone(event.surface)

    async def test_bot_no_legal_move_loses_one_life(
        self,
    ) -> None:
        room = create_room_snapshot(
            "bot-no-legal",
            ("alice",),
            mode=RoomMode.SOLO_BOT,
            permanent_bot_count=1,
            lives_per_player=3,
            now=NOW,
            seat_picker=lambda _count: 1,
        )
        handback = replace(
            room.players[0],
            controller=SeatController.BOT,
            handback_pending=True,
        )
        room = replace(room, players=(handback, room.players[1]))
        coordinator = RoomCoordinator(InMemoryRoomRepository([room]))

        outcome = await coordinator.finish_no_legal_move(
            room.room_id,
            1,
            expected_version=0,
            operation_id="runtime:no-legal-life",
            now=NOW,
        )

        assert outcome.snapshot is not None
        snapshot = outcome.snapshot
        self.assertEqual(snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(snapshot.remaining_lives, (3, 2))
        self.assertEqual(snapshot.eliminated_seats, ())
        self.assertEqual(snapshot.current_turn, 0)
        self.assertEqual(snapshot.players[0].controller, SeatController.HUMAN)
        self.assertFalse(snapshot.players[0].handback_pending)
        event = snapshot.life_loss_events[-1]
        self.assertEqual(event.reason, "no_legal_move")
        self.assertEqual(event.remaining_lives, 2)
        self.assertFalse(event.eliminated)


class LifePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_schema_round_trips_life_loss_event(self) -> None:
        room = pvp_with_lives(room_id="persist")
        coordinator = RoomCoordinator(InMemoryRoomRepository([room]))
        outcome = await coordinator.submit_user_turn(
            room.room_id,
            "alice",
            surface="りん",
            reading="りん",
            canonical_key="りん",
            expected_version=0,
            operation_id="persist-loss",
            now=NOW,
        )
        assert outcome.snapshot is not None

        document = serialize_room_snapshot(outcome.snapshot)

        self.assertEqual(
            document["room_repository_schema"],
            SNAPSHOT_SCHEMA_VERSION,
        )
        self.assertEqual(document["snapshot"]["lives_per_player"], 3)
        self.assertEqual(document["snapshot"]["remaining_lives"], [2, 3])
        self.assertEqual(
            document["snapshot"]["life_loss_events"][0]["surface"],
            "りん",
        )
        self.assertEqual(deserialize_room_snapshot(document), outcome.snapshot)

    def test_schema_three_defaults_to_one_life(self) -> None:
        room = pvp_with_lives(room_id="legacy", lives=1)
        document = deepcopy(serialize_room_snapshot(room))
        document["room_repository_schema"] = 3
        for field in (
            "rule_set",
            "lives_per_player",
            "remaining_lives",
            "life_loss_events",
        ):
            document["snapshot"].pop(field)

        restored = deserialize_room_snapshot(document)

        self.assertEqual(restored.lives_per_player, 1)
        self.assertEqual(restored.remaining_lives, (1, 1))
        self.assertEqual(restored.life_loss_events, ())

    def test_corrupt_remaining_lives_is_rejected(self) -> None:
        room = pvp_with_lives(room_id="corrupt")
        document = serialize_room_snapshot(room)
        document["snapshot"]["remaining_lives"] = [3, 0]

        with self.assertRaises(RoomSnapshotCorruptError):
            deserialize_room_snapshot(document)


class LifeLossRecordValidationTests(unittest.TestCase):
    def test_word_and_identity_fields_are_strictly_typed(self) -> None:
        valid = {
            "seat_index": 0,
            "reason": "timeout",
            "surface": None,
            "reading": None,
            "remaining_lives": 2,
            "eliminated": False,
            "occurred_at": NOW,
        }
        invalid_values = (
            ("seat_index", True),
            ("reason", 1),
            ("surface", 1),
            ("reading", 1),
        )

        for field, value in invalid_values:
            with self.subTest(field=field):
                arguments = dict(valid)
                arguments[field] = value
                with self.assertRaises(ValueError):
                    LifeLossRecord(**arguments)
