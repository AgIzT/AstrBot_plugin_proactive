from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ChatType = Literal["group", "private"]
ActionType = Literal["reply", "no_reply", "wait", "complete_talk"]


@dataclass(slots=True)
class NormalizedMessage:
    unified_msg_origin: str
    message_id: str
    sender_id: str
    sender_name: str
    self_id: str
    chat_type: ChatType
    content_text: str
    raw_summary: str
    created_at: float
    is_bot: bool = False
    is_mentioned: bool = False
    is_command_like: bool = False
    is_low_signal: bool = False


@dataclass(slots=True)
class SessionRecord:
    unified_msg_origin: str
    chat_type: ChatType
    enabled: bool = True
    last_read_at: float = 0.0
    last_active_at: float = 0.0
    consecutive_no_reply_count: int = 0
    talk_frequency_adjust: float = 1.0
    state: str = "idle"
    last_reply_target_message_id: str = ""
    last_reply_text_hash: str = ""
    last_reply_at: float = 0.0
    talk_value_override: float = -1.0
    cooldown_override: int = -1
    force_silent: bool = False


@dataclass(slots=True)
class ActionPlan:
    action: ActionType
    target_message_id: str = ""
    reason: str = ""
    unknown_words: list[str] = field(default_factory=list)
    question: str = ""
    quote: bool = False
    wait_seconds: int = 0


@dataclass(slots=True)
class ActionRecord:
    action: ActionType
    reason: str
    target_message_id: str
    created_at: float
    payload_summary: str = ""


@dataclass(slots=True)
class GroupPacingStats:
    unread_human_messages: int = 0
    recent_activity_messages: int = 0
    latest_human_message_at: float = 0.0
