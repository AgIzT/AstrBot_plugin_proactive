import unittest

from maibot_proactive.config import PluginConfig
from maibot_proactive.models import NormalizedMessage, SessionRecord
from maibot_proactive.policy import compute_group_trigger, should_observe_private


class DummyConfig(dict):
    def save_config(self):
        return None


def build_message(**overrides):
    data = {
        "unified_msg_origin": "onebot:group:123",
        "message_id": "m1",
        "sender_id": "u1",
        "sender_name": "alice",
        "self_id": "bot",
        "chat_type": "group",
        "content_text": "hello",
        "raw_summary": "hello",
        "created_at": 1000.0,
        "is_bot": False,
        "is_mentioned": False,
        "is_command_like": False,
    }
    data.update(overrides)
    return NormalizedMessage(**data)


class PolicyTests(unittest.TestCase):
    def test_group_mention_forces_observe(self):
        cfg = PluginConfig(DummyConfig())
        session = SessionRecord("onebot:group:123", "group")
        msg = build_message(is_mentioned=True)
        decision = compute_group_trigger(msg, session, 1, cfg, random_value=0.99, now=1000.0)
        self.assertTrue(decision.should_observe)
        self.assertEqual(decision.reason, "mentioned")

    def test_group_backoff_requires_more_unread_messages(self):
        cfg = PluginConfig(DummyConfig())
        session = SessionRecord("onebot:group:123", "group", consecutive_no_reply_count=5)
        msg = build_message()
        decision = compute_group_trigger(msg, session, 1, cfg, random_value=0.0, now=1000.0)
        self.assertFalse(decision.should_observe)
        self.assertEqual(decision.reason, "unread-threshold")

    def test_private_always_observes_normal_message(self):
        cfg = PluginConfig(DummyConfig())
        msg = build_message(chat_type="private", unified_msg_origin="onebot:private:1")
        decision = should_observe_private(msg, cfg)
        self.assertTrue(decision.should_observe)


if __name__ == "__main__":
    unittest.main()
