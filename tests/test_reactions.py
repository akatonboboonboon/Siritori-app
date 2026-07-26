from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from shiritori.rooms import (
    InMemoryRoomRepository,
    PlayerSeat,
    ReactionCapacityError,
    ReactionRateLimitError,
    Role,
    RoomAuthorizationError,
    RoomCoordinator,
    RoomEventKind,
    RoomMode,
    RoomNotFound,
    RoomSnapshot,
    RoomStatus,
    SeatController,
    UnsupportedReactionError,
)


NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def room(room_id: str = "reaction-room") -> RoomSnapshot:
    return RoomSnapshot(
        room_id=room_id,
        mode=RoomMode.PVP,
        status=RoomStatus.ACTIVE,
        players=(
            PlayerSeat(0, "alice", SeatController.HUMAN),
            PlayerSeat(1, "bob", SeatController.HUMAN),
        ),
        spectators=("viewer",),
        current_turn=0,
        state_version=7,
        expected_kana="り",
    )


class RoomReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_reaction_broadcasts_to_every_room_client_without_state_change(
        self,
    ) -> None:
        snapshot = room()
        repository = InMemoryRoomRepository((snapshot,))
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)
        alice_events = []
        bob_events = []
        viewer_events = []
        await coordinator.connect_client(
            snapshot.room_id, "alice", "alice-tab", alice_events.append
        )
        await coordinator.connect_client(
            snapshot.room_id, "bob", "bob-tab", bob_events.append
        )
        await coordinator.connect_client(
            snapshot.room_id, "viewer", "viewer-tab", viewer_events.append
        )
        alice_events.clear()
        bob_events.clear()
        viewer_events.clear()

        reaction = await coordinator.send_reaction(
            snapshot.room_id,
            "alice",
            "👍",
            now=NOW,
        )

        self.assertEqual(reaction.emoji, "👍")
        self.assertEqual(reaction.sender_user_id, "alice")
        self.assertIs(reaction.sender_role, Role.PLAYER)
        self.assertEqual(reaction.sent_at, NOW)
        for events in (alice_events, bob_events, viewer_events):
            self.assertEqual(len(events), 1)
            self.assertIs(events[0].kind, RoomEventKind.REACTION)
            self.assertEqual(events[0].room_id, snapshot.room_id)
            self.assertIsNone(events[0].snapshot)
            self.assertEqual(events[0].reaction, reaction)

        persisted = await repository.load(snapshot.room_id)
        self.assertEqual(persisted, snapshot)
        assert persisted is not None
        self.assertEqual(persisted.state_version, 7)
        self.assertEqual(persisted.history, ())

    async def test_spectator_can_react_and_is_identified_as_spectator(
        self,
    ) -> None:
        snapshot = room()
        coordinator = RoomCoordinator(
            InMemoryRoomRepository((snapshot,)),
            clock=lambda: NOW,
        )
        events = []
        await coordinator.connect_client(
            snapshot.room_id, "alice", "alice-tab", events.append
        )
        events.clear()

        reaction = await coordinator.send_reaction(
            snapshot.room_id,
            "viewer",
            "👏",
            now=NOW,
        )

        self.assertIs(reaction.sender_role, Role.SPECTATOR)
        self.assertEqual(events[-1].reaction, reaction)

    async def test_outsider_is_rejected_without_broadcast(self) -> None:
        snapshot = room()
        coordinator = RoomCoordinator(
            InMemoryRoomRepository((snapshot,)),
            clock=lambda: NOW,
        )
        events = []
        await coordinator.connect_client(
            snapshot.room_id, "alice", "alice-tab", events.append
        )
        events.clear()

        with self.assertRaisesRegex(
            RoomAuthorizationError, "not a room member"
        ):
            await coordinator.send_reaction(
                snapshot.room_id,
                "mallory",
                "😂",
                now=NOW,
            )

        self.assertEqual(events, [])
        self.assertNotIn(
            (snapshot.room_id, "mallory"),
            coordinator._reaction_sent_at,
        )

    async def test_free_text_and_unsupported_emoji_are_rejected(self) -> None:
        snapshot = room()
        coordinator = RoomCoordinator(
            InMemoryRoomRepository((snapshot,)),
            clock=lambda: NOW,
        )

        for unsupported in ("hello", "", "👍👍", "❤️", "🧨"):
            with self.subTest(unsupported=unsupported):
                with self.assertRaises(UnsupportedReactionError):
                    await coordinator.send_reaction(
                        snapshot.room_id,
                        "alice",
                        unsupported,
                        now=NOW,
                    )

        self.assertEqual(coordinator._reaction_sent_at, {})

    async def test_cooldown_is_scoped_to_room_and_user(self) -> None:
        first = room()
        second = room("another-room")
        coordinator = RoomCoordinator(
            InMemoryRoomRepository((first, second)),
            reaction_cooldown_seconds=1.0,
            clock=lambda: NOW,
        )

        await coordinator.send_reaction(
            first.room_id, "alice", "🔥", now=NOW
        )
        with self.assertRaises(ReactionRateLimitError) as caught:
            await coordinator.send_reaction(
                first.room_id,
                "alice",
                "👏",
                now=NOW + timedelta(milliseconds=250),
            )
        self.assertAlmostEqual(caught.exception.retry_after_seconds, 0.75)
        self.assertIn("retry after", str(caught.exception))

        await coordinator.send_reaction(
            first.room_id,
            "bob",
            "👏",
            now=NOW + timedelta(milliseconds=250),
        )
        await coordinator.send_reaction(
            second.room_id,
            "alice",
            "😮",
            now=NOW + timedelta(milliseconds=250),
        )
        await coordinator.send_reaction(
            first.room_id,
            "alice",
            "😂",
            now=NOW + timedelta(seconds=1),
        )

    async def test_missing_or_deleted_room_fails_safely(self) -> None:
        snapshot = room()
        repository = InMemoryRoomRepository((snapshot,))
        coordinator = RoomCoordinator(repository, clock=lambda: NOW)
        deleted = await repository.delete_if_version(
            snapshot.room_id,
            snapshot.state_version,
            "delete-for-reaction-test",
            command_fingerprint="0" * 64,
        )
        self.assertTrue(deleted.receipt and deleted.receipt.deleted)

        with self.assertRaises(RoomNotFound):
            await coordinator.send_reaction(
                snapshot.room_id,
                "alice",
                "👍",
                now=NOW,
            )
        self.assertEqual(coordinator._reaction_sent_at, {})

    async def test_hung_reaction_callback_is_cancelled_after_bounded_timeout(
        self,
    ) -> None:
        snapshot = room()
        active_callbacks = 0
        callback_count = 0
        callback_finished = asyncio.Event()
        never = asyncio.Event()

        async def hung_callback(event) -> None:
            nonlocal active_callbacks, callback_count
            if event.kind is not RoomEventKind.REACTION:
                return
            active_callbacks += 1
            callback_count += 1
            try:
                await never.wait()
            finally:
                active_callbacks -= 1
                callback_finished.set()

        coordinator = RoomCoordinator(
            InMemoryRoomRepository((snapshot,)),
            reaction_delivery_timeout_seconds=0.01,
            clock=lambda: NOW,
        )
        await coordinator.connect_client(
            snapshot.room_id,
            "alice",
            "hung-tab",
            hung_callback,
        )

        started = asyncio.get_running_loop().time()
        await coordinator.send_reaction(
            snapshot.room_id,
            "alice",
            "👍",
            now=NOW,
        )
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.wait_for(callback_finished.wait(), timeout=0.2)
        self.assertLess(elapsed, 0.2)
        self.assertEqual(active_callbacks, 0)

        callback_finished.clear()
        await coordinator.send_reaction(
            snapshot.room_id,
            "alice",
            "👏",
            now=NOW + timedelta(seconds=1),
        )
        await asyncio.wait_for(callback_finished.wait(), timeout=0.2)
        self.assertEqual(callback_count, 2)
        self.assertEqual(active_callbacks, 0)

    async def test_production_timestamp_is_sampled_after_authoritative_load(
        self,
    ) -> None:
        snapshot = room()
        current_time = [NOW]

        class AdvancingRepository(InMemoryRoomRepository):
            async def load(self, room_id: str):
                current_time[0] += timedelta(seconds=2)
                return await super().load(room_id)

        coordinator = RoomCoordinator(
            AdvancingRepository((snapshot,)),
            clock=lambda: current_time[0],
        )

        reaction = await coordinator.send_reaction(
            snapshot.room_id,
            "alice",
            "🔥",
        )

        self.assertEqual(reaction.sent_at, NOW + timedelta(seconds=2))
        self.assertEqual(
            coordinator._reaction_sent_at[(snapshot.room_id, "alice")],
            reaction.sent_at,
        )

    async def test_capacity_never_evicts_an_unexpired_cooldown(
        self,
    ) -> None:
        snapshot = room()
        coordinator = RoomCoordinator(
            InMemoryRoomRepository((snapshot,)),
            reaction_cooldown_seconds=1.0,
            reaction_rate_limit_capacity=2,
            clock=lambda: NOW,
        )

        await coordinator.send_reaction(
            snapshot.room_id,
            "alice",
            "👍",
            now=NOW,
        )
        await coordinator.send_reaction(
            snapshot.room_id,
            "bob",
            "👏",
            now=NOW,
        )
        with self.assertRaises(ReactionCapacityError) as capacity:
            await coordinator.send_reaction(
                snapshot.room_id,
                "viewer",
                "🔥",
                now=NOW,
            )
        self.assertAlmostEqual(capacity.exception.retry_after_seconds, 1.0)
        self.assertEqual(
            coordinator._reaction_sent_at,
            {
                (snapshot.room_id, "alice"): NOW,
                (snapshot.room_id, "bob"): NOW,
            },
        )

        with self.assertRaises(ReactionRateLimitError) as alice_cooldown:
            await coordinator.send_reaction(
                snapshot.room_id,
                "alice",
                "😂",
                now=NOW + timedelta(milliseconds=500),
            )
        self.assertIs(type(alice_cooldown.exception), ReactionRateLimitError)

        reaction = await coordinator.send_reaction(
            snapshot.room_id, "viewer", "😮", now=NOW + timedelta(seconds=1)
        )
        self.assertEqual(reaction.sender_user_id, "viewer")
        self.assertEqual(
            coordinator._reaction_sent_at,
            {(snapshot.room_id, "viewer"): NOW + timedelta(seconds=1)},
        )


if __name__ == "__main__":
    unittest.main()
