from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

from .models import ActionPlan, ActionRecord, NormalizedMessage, SessionRecord


class SQLiteStateStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    unified_msg_origin TEXT PRIMARY KEY,
                    chat_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_read_at REAL NOT NULL DEFAULT 0,
                    last_active_at REAL NOT NULL DEFAULT 0,
                    consecutive_no_reply_count INTEGER NOT NULL DEFAULT 0,
                    talk_frequency_adjust REAL NOT NULL DEFAULT 1.0,
                    state TEXT NOT NULL DEFAULT 'idle'
                );

                CREATE TABLE IF NOT EXISTS recent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    unified_msg_origin TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    is_mentioned INTEGER NOT NULL DEFAULT 0,
                    is_command_like INTEGER NOT NULL DEFAULT 0,
                    content_text TEXT NOT NULL,
                    raw_summary TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recent_messages_origin_time
                    ON recent_messages(unified_msg_origin, created_at);

                CREATE TABLE IF NOT EXISTS action_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unified_msg_origin TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    target_message_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_action_records_origin_time
                    ON action_records(unified_msg_origin, created_at);
                """
            )

    async def upsert_session(self, message: NormalizedMessage) -> SessionRecord:
        return await asyncio.to_thread(self._upsert_session_sync, message)

    def _upsert_session_sync(self, message: NormalizedMessage) -> SessionRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_sessions (
                    unified_msg_origin, chat_type, enabled, last_read_at,
                    last_active_at, consecutive_no_reply_count, talk_frequency_adjust, state
                ) VALUES (?, ?, 1, 0, 0, 0, 1.0, 'idle')
                ON CONFLICT(unified_msg_origin) DO UPDATE SET
                    chat_type = excluded.chat_type
                """,
                (message.unified_msg_origin, message.chat_type),
            )
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE unified_msg_origin = ?",
                (message.unified_msg_origin,),
            ).fetchone()
        return self._row_to_session(row)  # type: ignore[arg-type]

    async def get_session(self, origin: str, chat_type: str | None = None) -> SessionRecord:
        return await asyncio.to_thread(self._get_session_sync, origin, chat_type)

    def _get_session_sync(self, origin: str, chat_type: str | None = None) -> SessionRecord:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE unified_msg_origin = ?",
                (origin,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO chat_sessions (
                        unified_msg_origin, chat_type, enabled, last_read_at,
                        last_active_at, consecutive_no_reply_count, talk_frequency_adjust, state
                    ) VALUES (?, ?, 1, 0, 0, 0, 1.0, 'idle')
                    """,
                    (origin, chat_type or "group"),
                )
                row = conn.execute(
                    "SELECT * FROM chat_sessions WHERE unified_msg_origin = ?",
                    (origin,),
                ).fetchone()
        return self._row_to_session(row)  # type: ignore[arg-type]

    async def save_message(self, message: NormalizedMessage, max_context_messages: int) -> None:
        await asyncio.to_thread(self._save_message_sync, message, max_context_messages)

    def _save_message_sync(self, message: NormalizedMessage, max_context_messages: int) -> None:
        keep_count = max(max_context_messages * 2, 20)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recent_messages (
                    message_id, unified_msg_origin, sender_id, sender_name, is_bot,
                    is_mentioned, is_command_like, content_text, raw_summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.unified_msg_origin,
                    message.sender_id,
                    message.sender_name,
                    int(message.is_bot),
                    int(message.is_mentioned),
                    int(message.is_command_like),
                    message.content_text,
                    message.raw_summary,
                    message.created_at,
                ),
            )
            conn.execute(
                """
                DELETE FROM recent_messages
                WHERE id IN (
                    SELECT id FROM recent_messages
                    WHERE unified_msg_origin = ?
                    ORDER BY created_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (message.unified_msg_origin, keep_count),
            )

    async def get_recent_messages(self, origin: str, limit: int) -> list[NormalizedMessage]:
        return await asyncio.to_thread(self._get_recent_messages_sync, origin, limit)

    def _get_recent_messages_sync(self, origin: str, limit: int) -> list[NormalizedMessage]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT rm.*, cs.chat_type
                FROM recent_messages rm
                LEFT JOIN chat_sessions cs
                  ON cs.unified_msg_origin = rm.unified_msg_origin
                WHERE rm.unified_msg_origin = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (origin, limit),
            ).fetchall()
        return [self._row_to_message(origin, row) for row in rows]

    async def count_unread_human_messages(self, origin: str, since: float) -> int:
        return await asyncio.to_thread(self._count_unread_human_messages_sync, origin, since)

    def _count_unread_human_messages_sync(self, origin: str, since: float) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM recent_messages
                WHERE unified_msg_origin = ?
                  AND created_at > ?
                  AND is_bot = 0
                  AND is_command_like = 0
                """,
                (origin, since),
            ).fetchone()
        return int(row["cnt"]) if row else 0

    async def add_action_record(self, origin: str, plan: ActionPlan, created_at: float | None = None) -> None:
        await asyncio.to_thread(self._add_action_record_sync, origin, plan, created_at or time.time())

    def _add_action_record_sync(self, origin: str, plan: ActionPlan, created_at: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO action_records (
                    unified_msg_origin, action, reason, target_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (origin, plan.action, plan.reason, plan.target_message_id, created_at),
            )
            conn.execute(
                """
                DELETE FROM action_records
                WHERE id IN (
                    SELECT id FROM action_records
                    WHERE unified_msg_origin = ?
                    ORDER BY created_at DESC
                    LIMIT -1 OFFSET 30
                )
                """,
                (origin,),
            )

    async def get_recent_actions(self, origin: str, limit: int = 5) -> list[ActionRecord]:
        return await asyncio.to_thread(self._get_recent_actions_sync, origin, limit)

    def _get_recent_actions_sync(self, origin: str, limit: int) -> list[ActionRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT action, reason, target_message_id, created_at
                FROM action_records
                WHERE unified_msg_origin = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (origin, limit),
            ).fetchall()
        return [
            ActionRecord(
                action=row["action"],
                reason=row["reason"],
                target_message_id=row["target_message_id"],
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    async def mark_observed(self, origin: str, observed_at: float | None = None) -> None:
        await asyncio.to_thread(self._mark_observed_sync, origin, observed_at or time.time())

    def _mark_observed_sync(self, origin: str, observed_at: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE chat_sessions SET last_read_at = ? WHERE unified_msg_origin = ?",
                (observed_at, origin),
            )

    async def mark_reply_sent(self, origin: str, sent_at: float | None = None) -> None:
        await asyncio.to_thread(self._mark_reply_sent_sync, origin, sent_at or time.time())

    def _mark_reply_sent_sync(self, origin: str, sent_at: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_sessions
                SET last_active_at = ?, consecutive_no_reply_count = 0, state = 'idle'
                WHERE unified_msg_origin = ?
                """,
                (sent_at, origin),
            )

    async def increment_no_reply(self, origin: str) -> None:
        await asyncio.to_thread(self._increment_no_reply_sync, origin)

    def _increment_no_reply_sync(self, origin: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_sessions
                SET consecutive_no_reply_count = consecutive_no_reply_count + 1, state = 'idle'
                WHERE unified_msg_origin = ?
                """,
                (origin,),
            )

    async def set_waiting(self, origin: str, wake_at: float) -> None:
        await asyncio.to_thread(self._set_waiting_sync, origin, wake_at)

    def _set_waiting_sync(self, origin: str, wake_at: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE chat_sessions SET state = ? WHERE unified_msg_origin = ?",
                (f"waiting:{wake_at}", origin),
            )

    async def clear_waiting(self, origin: str) -> None:
        await asyncio.to_thread(self._clear_waiting_sync, origin)

    def _clear_waiting_sync(self, origin: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE chat_sessions SET state = 'idle' WHERE unified_msg_origin = ?",
                (origin,),
            )

    def _row_to_session(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            unified_msg_origin=row["unified_msg_origin"],
            chat_type=row["chat_type"],
            enabled=bool(row["enabled"]),
            last_read_at=float(row["last_read_at"]),
            last_active_at=float(row["last_active_at"]),
            consecutive_no_reply_count=int(row["consecutive_no_reply_count"]),
            talk_frequency_adjust=float(row["talk_frequency_adjust"]),
            state=str(row["state"]),
        )

    def _row_to_message(self, origin: str, row: sqlite3.Row) -> NormalizedMessage:
        chat_type = str(row["chat_type"] or "private")
        return NormalizedMessage(
            unified_msg_origin=origin,
            message_id=str(row["message_id"]),
            sender_id=str(row["sender_id"]),
            sender_name=str(row["sender_name"]),
            self_id="",
            chat_type=chat_type,  # type: ignore[arg-type]
            content_text=str(row["content_text"]),
            raw_summary=str(row["raw_summary"]),
            created_at=float(row["created_at"]),
            is_bot=bool(row["is_bot"]),
            is_mentioned=bool(row["is_mentioned"]),
            is_command_like=bool(row["is_command_like"]),
        )
