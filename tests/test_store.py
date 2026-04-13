import tempfile
import unittest
from pathlib import Path

from maibot_proactive.models import ActionPlan, NormalizedMessage
from maibot_proactive.store import SQLiteStateStore


def build_message(message_id: str, created_at: float):
    return NormalizedMessage(
        unified_msg_origin="onebot:group:123",
        message_id=message_id,
        sender_id="u1",
        sender_name="alice",
        self_id="bot",
        chat_type="group",
        content_text=f"msg-{message_id}",
        raw_summary=f"msg-{message_id}",
        created_at=created_at,
    )


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
            ActionPlan(action="no_reply", target_message_id="m1", reason="test"),
            created_at=123.0,
        )
        actions = await self.store.get_recent_actions("onebot:group:123")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "no_reply")
        self.assertEqual(actions[0].target_message_id, "m1")


if __name__ == "__main__":
    unittest.main()
