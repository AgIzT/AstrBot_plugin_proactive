from __future__ import annotations

from dataclasses import dataclass

from .config import PluginConfig
from .models import NormalizedMessage, SessionRecord

MIN_TALK_FREQUENCY_ADJUST = 0.45
MAX_TALK_FREQUENCY_ADJUST = 1.35


@dataclass(slots=True)
class TriggerDecision:
    should_observe: bool
    reason: str = ""
    effective_talk_value: float = 0.0
    heat_factor: float = 1.0


def should_ignore_message(message: NormalizedMessage, config: PluginConfig) -> bool:
    if not config.enabled:
        return True
    if config.is_blocked(message.unified_msg_origin):
        return True
    if message.is_bot:
        return True
    if config.ignore_command_like_messages and message.is_command_like:
        return True
    if config.ignore_low_signal_messages and is_low_signal_message(message):
        return True
    return False


def is_low_signal_message(message: NormalizedMessage) -> bool:
    if message.is_low_signal:
        return True

    summary = message.raw_summary.strip().lower()
    if not summary or summary == "[empty-message]":
        return True

    media_tokens = {"[image]", "[voice]", "[video]", "[emoji]", "[poke]"}
    parts = [part for part in summary.split() if part]
    if parts and all(part in media_tokens for part in parts):
        return True

    if len(summary) <= 1 and summary not in {"?", "!"}:
        return True

    alnum_len = len("".join(ch for ch in summary if ch.isalnum()))
    return alnum_len <= 1 and summary not in {"?", "!"}


def compute_group_trigger(
    message: NormalizedMessage,
    session: SessionRecord,
    unread_human_messages: int,
    config: PluginConfig,
    random_value: float,
    now: float,
) -> TriggerDecision:
    if not config.enable_group:
        return TriggerDecision(False, "group-disabled")
    if should_ignore_message(message, config):
        return TriggerDecision(False, "ignored")
    if session.force_silent:
        return TriggerDecision(False, "force-silent")
    if config.enable_session_overrides and not session.enabled:
        return TriggerDecision(False, "session-disabled")
    if message.is_mentioned and config.mention_force_reply:
        return TriggerDecision(True, "mentioned")
    if not message.raw_summary.strip():
        return TriggerDecision(False, "empty-summary")

    cooldown_seconds = get_effective_cooldown_seconds(session, config)
    if session.last_active_at and (now - session.last_active_at) < cooldown_seconds:
        return TriggerDecision(False, "cooldown")

    threshold = get_unread_threshold(session.consecutive_no_reply_count, random_value)
    if unread_human_messages < threshold:
        return TriggerDecision(False, "unread-threshold")

    heat_factor = get_heat_factor(unread_human_messages)
    chance = max(
        0.0,
        min(
            1.0,
            get_effective_group_talk_value(session, config) * session.talk_frequency_adjust * heat_factor,
        ),
    )
    if random_value < chance:
        return TriggerDecision(True, "probability", effective_talk_value=chance, heat_factor=heat_factor)
    return TriggerDecision(False, "probability-miss", effective_talk_value=chance, heat_factor=heat_factor)


def should_observe_private(
    message: NormalizedMessage,
    config: PluginConfig,
    session: SessionRecord | None = None,
) -> TriggerDecision:
    if not config.enable_private:
        return TriggerDecision(False, "private-disabled")
    if should_ignore_message(message, config):
        return TriggerDecision(False, "ignored")
    if session and session.force_silent:
        return TriggerDecision(False, "force-silent")
    if session and config.enable_session_overrides and not session.enabled:
        return TriggerDecision(False, "session-disabled")
    return TriggerDecision(True, "private-message")


def get_effective_group_talk_value(session: SessionRecord, config: PluginConfig) -> float:
    if config.enable_session_overrides and session.talk_value_override >= 0:
        return session.talk_value_override
    return config.group_talk_value


def get_effective_cooldown_seconds(session: SessionRecord, config: PluginConfig) -> int:
    if config.enable_session_overrides and session.cooldown_override >= 0:
        return session.cooldown_override
    return config.group_reply_cooldown_seconds


def get_unread_threshold(consecutive_no_reply_count: int, random_value: float) -> int:
    if consecutive_no_reply_count >= 5:
        return 2
    if consecutive_no_reply_count >= 3:
        return 2 if random_value < 0.5 else 1
    return 1


def get_heat_factor(unread_human_messages: int) -> float:
    if unread_human_messages >= 3:
        return 1.15
    if unread_human_messages == 2:
        return 1.08
    return 1.0


def clamp_talk_frequency_adjust(value: float) -> float:
    return max(MIN_TALK_FREQUENCY_ADJUST, min(MAX_TALK_FREQUENCY_ADJUST, value))
