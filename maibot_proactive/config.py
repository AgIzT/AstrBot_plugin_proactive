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
        raw_blocked = cfg.get("blocked_origins", []) or []
        if not isinstance(raw_blocked, Iterable) or isinstance(raw_blocked, (str, bytes)):
            raw_blocked = []
        self.blocked_origins = [str(item).strip() for item in raw_blocked if str(item).strip()]

    def is_blocked(self, origin: str) -> bool:
        return origin in set(self.blocked_origins)
