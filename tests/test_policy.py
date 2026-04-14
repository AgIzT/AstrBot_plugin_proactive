import unittest

from maibot_proactive.config import PluginConfig
from maibot_proactive.models import GroupPacingStats, NormalizedMessage, SessionRecord
from maibot_proactive.policy import (
    clamp_talk_frequency_adjust,
    compute_group_trigger,
    compute_pacing_snapshot,
    get_activity_factor,
    get_effective_cooldown_seconds,
    get_effective_group_talk_value,
    get_heat_factor,
    get_silence_factor,
    should_observe_private,
)


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
        "is_low_signal": False,
    }
    data.update(overrides)
    return NormalizedMessage(**data)


class PolicyTests(unittest.TestCase):
    def test_group_mention_forces_observe(self):
        cfg = PluginConfig(DummyConfig())
        session = SessionRecord("onebot:group:123", "group")
        msg = build_message(is_mentioned=True)
        stats = GroupPacingStats(unread_human_messages=1, recent_activity_messages=1)
        decision = compute_group_trigger(msg, session, stats, cfg, random_value=0.99, now=1000.0)
        self.assertTrue(decision.should_observe)
        self.assertEqual(decision.reason, "mentioned")
        self.assertTrue(decision.probability_hit)

    def test_group_backoff_requires_more_unread_messages(self):
        cfg = PluginConfig(DummyConfig())
        session = SessionRecord("onebot:group:123", "group", consecutive_no_reply_count=5)
        msg = build_message()
        stats = GroupPacingStats(unread_human_messages=1, recent_activity_messages=1)
        decision = compute_group_trigger(msg, session, stats, cfg, random_value=0.0, now=1000.0)
        self.assertFalse(decision.should_observe)
        self.assertEqual(decision.reason, "unread-threshold")
        self.assertTrue(decision.unread_threshold_hit)

    def test_group_cooldown_blocks_trigger(self):
        cfg = PluginConfig(DummyConfig())
        session = SessionRecord("onebot:group:123", "group", last_active_at=995.0)
        msg = build_message()
        stats = GroupPacingStats(unread_human_messages=3, recent_activity_messages=5)
        decision = compute_group_trigger(msg, session, stats, cfg, random_value=0.0, now=1000.0)
        self.assertFalse(decision.should_observe)
        self.assertEqual(decision.reason, "cooldown")
        self.assertTrue(decision.cooldown_hit)

    def test_private_always_observes_normal_message(self):
        cfg = PluginConfig(DummyConfig())
        msg = build_message(chat_type="private", unified_msg_origin="onebot:private:1")
        decision = should_observe_private(msg, cfg)
        self.assertTrue(decision.should_observe)

    def test_low_signal_private_message_is_ignored(self):
        cfg = PluginConfig(DummyConfig())
        msg = build_message(
            chat_type="private",
            unified_msg_origin="onebot:private:1",
            raw_summary="[emoji]",
            is_low_signal=True,
        )
        decision = should_observe_private(msg, cfg)
        self.assertFalse(decision.should_observe)
        self.assertEqual(decision.reason, "ignored")

    def test_group_uses_session_override_values(self):
        cfg = PluginConfig(DummyConfig())
        session = SessionRecord(
            "onebot:group:123",
            "group",
            talk_value_override=0.42,
            cooldown_override=9,
        )
        self.assertEqual(get_effective_group_talk_value(session, cfg), 0.42)
        self.assertEqual(get_effective_cooldown_seconds(session, cfg), 9)

    def test_heat_activity_and_silence_factors_use_expected_bands(self):
        self.assertEqual(get_heat_factor(1), 1.0)
        self.assertEqual(get_heat_factor(2), 1.08)
        self.assertEqual(get_heat_factor(3), 1.15)
        self.assertEqual(get_activity_factor(1), 0.95)
        self.assertEqual(get_activity_factor(4), 1.08)
        self.assertEqual(get_activity_factor(7), 1.15)
        self.assertEqual(get_silence_factor(2), 1.0)
        self.assertEqual(get_silence_factor(3), 0.92)
        self.assertEqual(get_silence_factor(5), 0.82)

    def test_snapshot_combines_all_pacing_factors(self):
        cfg = PluginConfig(DummyConfig({"group_talk_value": 0.2}))
        session = SessionRecord("onebot:group:123", "group", talk_frequency_adjust=1.1, consecutive_no_reply_count=4)
        stats = GroupPacingStats(unread_human_messages=3, recent_activity_messages=5)
        snapshot = compute_pacing_snapshot(session, stats, cfg)
        self.assertAlmostEqual(snapshot.base_talk_value, 0.2)
        self.assertAlmostEqual(snapshot.heat_factor, 1.15)
        self.assertAlmostEqual(snapshot.activity_factor, 1.08)
        self.assertAlmostEqual(snapshot.silence_factor, 0.92)
        self.assertAlmostEqual(snapshot.effective_probability, 0.2 * 1.1 * 1.15 * 1.08 * 0.92, places=6)

    def test_talk_frequency_clamp_has_configurable_bounds(self):
        self.assertEqual(clamp_talk_frequency_adjust(0.1), 0.45)
        self.assertEqual(clamp_talk_frequency_adjust(2.0), 1.35)
        self.assertEqual(clamp_talk_frequency_adjust(1.1), 1.1)
        self.assertEqual(clamp_talk_frequency_adjust(0.2, minimum=0.3, maximum=0.9), 0.3)


if __name__ == "__main__":
    unittest.main()
