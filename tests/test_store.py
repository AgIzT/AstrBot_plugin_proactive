import tempfile
import unittest
from pathlib import Path

from maibot_proactive.models import ActionPlan, NormalizedMessage
from maibot_proactive.store import SQLiteStateStore


def build_message(message_id: str, created_at: float, **overrides):
    data = {
        "unified_msg_origin": "onebot:group:123",
        "message_id": message_id,
        "sender_id": "u1",
        "sender_name": "alice",
        "self_id": "bot",
        "chat_type": "group",
        "content_text": f"msg-{message_id}",
        "raw_summary": f"msg-{message_id}",
        "created_at": created_at,
        "is_low_signal": False,
    }
    data.update(overrides)
    return NormalizedMessage(**data)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp_dir.name) / "test.sqlite3")

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_recent_message_pruning(self):
        first = build_message("m1", 1.0)
        await self.store.upsert_session(first)
        for idx in range(8):
            await self.store.save_message(build_message(f"m{idx}", float(idx)), max_context_messages=2)
        messages = await self.store.get_recent_messages("onebot:group:123", 20)
        self.assertLessEqual(len(messages), 20)
        self.assertGreaterEqual(len(messages), 4)

    async def test_action_record_roundtrip(self):
        await self.store.add_action_record(
            "onebot:group:123",
            ActionPlan(action="no_reply", target_message_id="m1", reason="test", question="hello"),
            created_at=123.0,
        )
        actions = await self.store.get_recent_actions("onebot:group:123")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "no_reply")
        self.assertEqual(actions[0].target_message_id, "m1")
        self.assertIn("question=hello", actions[0].payload_summary)

    async def test_unread_count_ignores_low_signal_messages(self):
        message = build_message("m1", 1.0)
        await self.store.upsert_session(message)
        await self.store.save_message(message, max_context_messages=5)
        await self.store.save_message(build_message("m2", 2.0, is_low_signal=True), max_context_messages=5)
        unread = await self.store.count_unread_human_messages("onebot:group:123", 0.0)
        self.assertEqual(unread, 1)

    async def test_adjust_talk_frequency_is_clamped(self):
        message = build_message("m1", 1.0)
        await self.store.upsert_session(message)
        lowered = await self.store.adjust_talk_frequency("onebot:group:123", -9.0)
        raised = await self.store.adjust_talk_frequency("onebot:group:123", 9.0)
        self.assertEqual(lowered, 0.45)
        self.assertEqual(raised, 1.35)

    async def test_duplicate_reply_detection_uses_target_and_hash(self):
        message = build_message("m1", 1.0)
        await self.store.upsert_session(message)
        await self.store.mark_reply_sent(
            "onebot:group:123",
            sent_at=100.0,
            target_message_id="m1",
            reply_text_hash="hash-a",
        )
        self.assertTrue(
            await self.store.is_duplicate_reply(
                "onebot:group:123",
                "m1",
                "hash-b",
                now=120.0,
                within_seconds=180,
            )
        )
        self.assertTrue(
            await self.store.is_duplicate_reply(
                "onebot:group:123",
                "m2",
                "hash-a",
                now=120.0,
                within_seconds=180,
            )
        )
        self.assertFalse(
            await self.store.is_duplicate_reply(
                "onebot:group:123",
                "m2",
                "hash-b",
                now=400.0,
                within_seconds=180,
            )
        )


if __name__ == "__main__":
    unittest.main()
