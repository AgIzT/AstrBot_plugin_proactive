from __future__ import annotations

from dataclasses import dataclass

from .config import PluginConfig
from .models import NormalizedMessage, SessionRecord


@dataclass(slots=True)
class TriggerDecision:
    should_observe: bool
    reason: str = ""


def should_ignore_message(message: NormalizedMessage, config: PluginConfig) -> bool:
    if not config.enabled:
        return True
    if config.is_blocked(message.unified_msg_origin):
        return True
    if message.is_bot:
        return True
    if config.ignore_command_like_messages and message.is_command_like:
        return True
    return False


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
    if message.is_mentioned and config.mention_force_reply:
        return TriggerDecision(True, "mentioned")
    if not message.raw_summary.strip():
        return TriggerDecision(False, "empty-summary")
    if session.last_active_at and (now - session.last_active_at) < config.group_reply_cooldown_seconds:
        return TriggerDecision(False, "cooldown")

    if session.consecutive_no_reply_count >= 5:
        threshold = 2
    elif session.consecutive_no_reply_count >= 3:
        threshold = 2 if random_value < 0.5 else 1
    else:
        threshold = 1

    if unread_human_messages < threshold:
        return TriggerDecision(False, "unread-threshold")

    chance = max(0.0, min(1.0, config.group_talk_value * session.talk_frequency_adjust))
    if random_value < chance:
        return TriggerDecision(True, "probability")
    return TriggerDecision(False, "probability-miss")


def should_observe_private(message: NormalizedMessage, config: PluginConfig) -> TriggerDecision:
    if not config.enable_private:
        return TriggerDecision(False, "private-disabled")
    if should_ignore_message(message, config):
        return TriggerDecision(False, "ignored")
    return TriggerDecision(True, "private-message")
