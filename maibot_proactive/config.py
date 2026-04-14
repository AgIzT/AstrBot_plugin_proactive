from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(slots=True)
class PluginConfig:
    raw_config: Any
    enabled: bool = True
    enable_group: bool = True
    enable_private: bool = True
    fallback_provider_id: str = ""
    group_talk_value: float = 0.18
    mention_force_reply: bool = True
    group_reply_cooldown_seconds: int = 45
    private_wait_default_seconds: int = 5
    max_context_messages: int = 20
    write_back_to_conversation: bool = True
    ignore_command_like_messages: bool = True
    enable_session_overrides: bool = True
    duplicate_reply_window_seconds: int = 180
    log_decisions: bool = True
    ignore_low_signal_messages: bool = True
    pacing_activity_window_seconds: int = 90
    pacing_recovery_after_seconds: int = 600
    pacing_mention_boost: float = 0.20
    pacing_activity_boost: float = 0.06
    pacing_reply_decay: float = 0.18
    pacing_no_reply_decay: float = 0.08
    pacing_frequency_min: float = 0.45
    pacing_frequency_max: float = 1.35
    whitelist_origins: list[str] = None  # type: ignore[assignment]
    blocked_origins: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        cfg = self.raw_config
        self.enabled = bool(cfg.get("enabled", True))
        self.enable_group = bool(cfg.get("enable_group", True))
        self.enable_private = bool(cfg.get("enable_private", True))
        self.fallback_provider_id = str(cfg.get("fallback_provider_id", "") or "").strip()
        self.group_talk_value = float(cfg.get("group_talk_value", 0.18) or 0.18)
        self.mention_force_reply = bool(cfg.get("mention_force_reply", True))
        self.group_reply_cooldown_seconds = max(
            0,
            int(cfg.get("group_reply_cooldown_seconds", 45) or 45),
        )
        self.private_wait_default_seconds = max(
            1,
            int(cfg.get("private_wait_default_seconds", 5) or 5),
        )
        self.max_context_messages = max(1, int(cfg.get("max_context_messages", 20) or 20))
        self.write_back_to_conversation = bool(cfg.get("write_back_to_conversation", True))
        self.ignore_command_like_messages = bool(cfg.get("ignore_command_like_messages", True))
        self.enable_session_overrides = bool(cfg.get("enable_session_overrides", True))
        self.duplicate_reply_window_seconds = max(
            1,
            int(cfg.get("duplicate_reply_window_seconds", 180) or 180),
        )
        self.log_decisions = bool(cfg.get("log_decisions", True))
        self.ignore_low_signal_messages = bool(cfg.get("ignore_low_signal_messages", True))
        self.pacing_activity_window_seconds = max(
            10,
            int(cfg.get("pacing_activity_window_seconds", 90) or 90),
        )
        self.pacing_recovery_after_seconds = max(
            60,
            int(cfg.get("pacing_recovery_after_seconds", 600) or 600),
        )
        self.pacing_mention_boost = max(0.0, float(cfg.get("pacing_mention_boost", 0.20) or 0.20))
        self.pacing_activity_boost = max(0.0, float(cfg.get("pacing_activity_boost", 0.06) or 0.06))
        self.pacing_reply_decay = max(0.0, float(cfg.get("pacing_reply_decay", 0.18) or 0.18))
        self.pacing_no_reply_decay = max(0.0, float(cfg.get("pacing_no_reply_decay", 0.08) or 0.08))
        self.pacing_frequency_min = max(
            0.05,
            float(cfg.get("pacing_frequency_min", 0.45) or 0.45),
        )
        self.pacing_frequency_max = max(
            self.pacing_frequency_min,
            float(cfg.get("pacing_frequency_max", 1.35) or 1.35),
        )
        raw_whitelist = cfg.get("whitelist_origins", []) or []
        if not isinstance(raw_whitelist, Iterable) or isinstance(raw_whitelist, (str, bytes)):
            raw_whitelist = []
        self.whitelist_origins = [
            str(item).strip() for item in raw_whitelist if str(item).strip()
        ]
        raw_blocked = cfg.get("blocked_origins", []) or []
        if not isinstance(raw_blocked, Iterable) or isinstance(raw_blocked, (str, bytes)):
            raw_blocked = []
        self.blocked_origins = [str(item).strip() for item in raw_blocked if str(item).strip()]

    def use_whitelist_mode(self) -> bool:
        return bool(self.whitelist_origins)

    def is_allowed_origin(self, origin: str) -> bool:
        if self.use_whitelist_mode():
            return origin in set(self.whitelist_origins)
        return origin not in set(self.blocked_origins)

    def is_blocked(self, origin: str) -> bool:
        return not self.is_allowed_origin(origin)
