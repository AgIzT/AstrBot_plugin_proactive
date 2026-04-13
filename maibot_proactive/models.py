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
