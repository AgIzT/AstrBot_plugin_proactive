from __future__ import annotations

from dataclasses import dataclass, field

from .config import PluginConfig
from .models import GroupPacingStats, NormalizedMessage, SessionRecord

DEFAULT_MIN_TALK_FREQUENCY_ADJUST = 0.45
DEFAULT_MAX_TALK_FREQUENCY_ADJUST = 1.35
HOT_ACTIVITY_THRESHOLD = 4
RECOVERY_STEP = 0.05


@dataclass(slots=True)
class PacingSnapshot:
    base_talk_value: float
    talk_frequency_adjust: float
    heat_factor: float
    activity_factor: float
    silence_factor: float
    effective_probability: float
    unread_human_messages: int
    recent_activity_messages: int
    reason_tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TriggerDecision:
    should_observe: bool
    reason: str = ""
    effective_talk_value: float = 0.0
    heat_factor: float = 1.0
    activity_factor: float = 1.0
    silence_factor: float = 1.0
    cooldown_hit: bool = False
    unread_threshold_hit: bool = False
    probability_hit: bool = False
    snapshot: PacingSnapshot | None = None


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
    pacing_stats: GroupPacingStats,
    config: PluginConfig,
    random_value: float,
    now: float,
) -> TriggerDecision:
    snapshot = compute_pacing_snapshot(session, pacing_stats, config)

    if not config.enable_group:
        return _decision(False, "group-disabled", snapshot)
    if should_ignore_message(message, config):
        return _decision(False, "ignored", snapshot)
    if session.force_silent:
        return _decision(False, "force-silent", snapshot)
    if config.enable_session_overrides and not session.enabled:
        return _decision(False, "session-disabled", snapshot)
    if message.is_mentioned and config.mention_force_reply:
        snapshot.reason_tags.append("mentioned")
        return _decision(True, "mentioned", snapshot, probability_hit=True)
    if not message.raw_summary.strip():
        return _decision(False, "empty-summary", snapshot)

    cooldown_seconds = get_effective_cooldown_seconds(session, config)
    if session.last_active_at and (now - session.last_active_at) < cooldown_seconds:
        snapshot.reason_tags.append("cooldown")
        return _decision(False, "cooldown", snapshot, cooldown_hit=True)

    threshold = get_unread_threshold(session.consecutive_no_reply_count, random_value)
    if pacing_stats.unread_human_messages < threshold:
        snapshot.reason_tags.append("unread-threshold")
        return _decision(False, "unread-threshold", snapshot, unread_threshold_hit=True)

    probability_hit = random_value < snapshot.effective_probability
    snapshot.reason_tags.append("probability-hit" if probability_hit else "probability-miss")
    return _decision(
        probability_hit,
        "probability" if probability_hit else "probability-miss",
        snapshot,
        probability_hit=probability_hit,
    )


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


def compute_pacing_snapshot(
    session: SessionRecord,
    pacing_stats: GroupPacingStats,
    config: PluginConfig,
) -> PacingSnapshot:
    base_talk_value = get_effective_group_talk_value(session, config)
    heat_factor = get_heat_factor(pacing_stats.unread_human_messages)
    activity_factor = get_activity_factor(pacing_stats.recent_activity_messages)
    silence_factor = get_silence_factor(session.consecutive_no_reply_count)
    effective_probability = max(
        0.0,
        min(
            1.0,
            base_talk_value
            * session.talk_frequency_adjust
            * heat_factor
            * activity_factor
            * silence_factor,
        ),
    )
    reason_tags = [
        f"base={base_talk_value:.3f}",
        f"freq={session.talk_frequency_adjust:.3f}",
        f"heat={heat_factor:.2f}",
        f"activity={activity_factor:.2f}",
        f"silence={silence_factor:.2f}",
    ]
    return PacingSnapshot(
        base_talk_value=base_talk_value,
        talk_frequency_adjust=session.talk_frequency_adjust,
        heat_factor=heat_factor,
        activity_factor=activity_factor,
        silence_factor=silence_factor,
        effective_probability=effective_probability,
        unread_human_messages=pacing_stats.unread_human_messages,
        recent_activity_messages=pacing_stats.recent_activity_messages,
        reason_tags=reason_tags,
    )


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


def get_activity_factor(recent_activity_messages: int) -> float:
    if recent_activity_messages >= 7:
        return 1.15
    if recent_activity_messages >= 4:
        return 1.08
    if recent_activity_messages >= 2:
        return 1.0
    return 0.95


def get_silence_factor(consecutive_no_reply_count: int) -> float:
    if consecutive_no_reply_count >= 5:
        return 0.82
    if consecutive_no_reply_count >= 3:
        return 0.92
    return 1.0


def is_hot_activity(recent_activity_messages: int) -> bool:
    return recent_activity_messages >= HOT_ACTIVITY_THRESHOLD


def clamp_talk_frequency_adjust(
    value: float,
    minimum: float = DEFAULT_MIN_TALK_FREQUENCY_ADJUST,
    maximum: float = DEFAULT_MAX_TALK_FREQUENCY_ADJUST,
) -> float:
    if maximum < minimum:
        maximum = minimum
    return max(minimum, min(maximum, value))


def _decision(
    should_observe: bool,
    reason: str,
    snapshot: PacingSnapshot,
    *,
    cooldown_hit: bool = False,
    unread_threshold_hit: bool = False,
    probability_hit: bool = False,
) -> TriggerDecision:
    return TriggerDecision(
        should_observe=should_observe,
        reason=reason,
        effective_talk_value=snapshot.effective_probability,
        heat_factor=snapshot.heat_factor,
        activity_factor=snapshot.activity_factor,
        silence_factor=snapshot.silence_factor,
        cooldown_hit=cooldown_hit,
        unread_threshold_hit=unread_threshold_hit,
        probability_hit=probability_hit,
        snapshot=snapshot,
    )
