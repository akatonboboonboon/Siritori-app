from __future__ import annotations

from datetime import datetime, timezone
import unittest

from shiritori.rooms import (
    InMemoryRoomRepository,
    PlayerSeat,
    Role,
    RoomCoordinator,
    RoomMode,
    RoomSnapshot,
    RoomStatus,
    SeatController,
    TurnRecord,
)


NOW = datetime(2026, 7, 25, 3, 0, tzinfo=timezone.utc)


class MultiplayerLossReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_eliminates_one_player_in_three_player_match(
        self,
    ) -> None:
        snapshot = RoomSnapshot(
            room_id="three-player-duplicate",
            mode=RoomMode.PVP,
            status=RoomStatus.ACTIVE,
            players=tuple(
                PlayerSeat(index, user_id, SeatController.HUMAN)
                for index, user_id in enumerate(("alice", "bob", "carol"))
            ),
            current_turn=0,
            history=(
                TurnRecord(
                    surface="\u3042\u304b",
                    reading="\u3042\u304b",
                    canonical_key="\u3042\u304b",
                    seat_index=2,
                    actor_user_id="carol",
                    by_bot=False,
                    submitted_at=NOW,
                ),
            ),
            expected_kana="\u3042",
        )
        coordinator = RoomCoordinator(InMemoryRoomRepository([snapshot]))

        outcome = await coordinator.submit_user_turn(
            snapshot.room_id,
            "alice",
            surface="\u3042\u304b",
            reading="\u3042\u304b",
            canonical_key="\u3042\u304b",
            expected_version=0,
            operation_id="duplicate-elimination",
            now=NOW,
        )

        assert outcome.snapshot is not None
        self.assertEqual(outcome.snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(outcome.snapshot.eliminated_seats, (0,))
        self.assertEqual(outcome.snapshot.current_turn, 1)
        self.assertEqual(len(outcome.snapshot.history), 1)
        self.assertEqual(outcome.snapshot.role_for_user("alice"), Role.SPECTATOR)

    async def test_no_legal_move_eliminates_only_bot_controlled_player(
        self,
    ) -> None:
        snapshot = RoomSnapshot(
            room_id="three-player-no-legal",
            mode=RoomMode.PVP,
            status=RoomStatus.ACTIVE,
            players=(
                PlayerSeat(0, "alice", SeatController.HUMAN),
                PlayerSeat(1, "bob", SeatController.BOT),
                PlayerSeat(2, "carol", SeatController.HUMAN),
            ),
            current_turn=1,
            expected_kana="\u304b",
            turn_seconds=30,
        )
        coordinator = RoomCoordinator(InMemoryRoomRepository([snapshot]))

        outcome = await coordinator.finish_no_legal_move(
            snapshot.room_id,
            1,
            expected_version=0,
            operation_id="runtime:no-legal-three-player",
            now=NOW,
        )

        assert outcome.snapshot is not None
        self.assertEqual(outcome.snapshot.status, RoomStatus.ACTIVE)
        self.assertEqual(outcome.snapshot.eliminated_seats, (1,))
        self.assertEqual(outcome.snapshot.current_turn, 2)
        self.assertEqual(outcome.snapshot.expected_kana, "\u304b")
        self.assertEqual(outcome.snapshot.role_for_user("bob"), Role.SPECTATOR)


if __name__ == "__main__":
    unittest.main()
