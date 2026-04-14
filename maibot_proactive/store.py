from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

from .models import ActionPlan, ActionRecord, GroupPacingStats, NormalizedMessage, SessionRecord
from .policy import RECOVERY_STEP, clamp_talk_frequency_adjust


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
                    state TEXT NOT NULL DEFAULT 'idle',
                    last_reply_target_message_id TEXT NOT NULL DEFAULT '',
                    last_reply_text_hash TEXT NOT NULL DEFAULT '',
                    last_reply_at REAL NOT NULL DEFAULT 0,
                    talk_value_override REAL NOT NULL DEFAULT -1.0,
                    cooldown_override INTEGER NOT NULL DEFAULT -1,
                    force_silent INTEGER NOT NULL DEFAULT 0
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
                    is_low_signal INTEGER NOT NULL DEFAULT 0,
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
                    payload_summary TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_action_records_origin_time
                    ON action_records(unified_msg_origin, created_at);
                """
            )
            self._ensure_column(conn, "chat_sessions", "last_reply_target_message_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "chat_sessions", "last_reply_text_hash", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "chat_sessions", "last_reply_at", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "chat_sessions", "talk_value_override", "REAL NOT NULL DEFAULT -1.0")
            self._ensure_column(conn, "chat_sessions", "cooldown_override", "INTEGER NOT NULL DEFAULT -1")
            self._ensure_column(conn, "chat_sessions", "force_silent", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "recent_messages", "is_low_signal", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "action_records", "payload_summary", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")

    async def upsert_session(self, message: NormalizedMessage) -> SessionRecord:
        return await asyncio.to_thread(self._upsert_session_sync, message)

    def _upsert_session_sync(self, message: NormalizedMessage) -> SessionRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_sessions (
                    unified_msg_origin, chat_type, enabled, last_read_at,
                    last_active_at, consecutive_no_reply_count, talk_frequency_adjust, state,
                    last_reply_target_message_id, last_reply_text_hash, last_reply_at,
                    talk_value_override, cooldown_override, force_silent
                ) VALUES (?, ?, 1, 0, 0, 0, 1.0, 'idle', '', '', 0, -1.0, -1, 0)
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
                        last_active_at, consecutive_no_reply_count, talk_frequency_adjust, state,
                        last_reply_target_message_id, last_reply_text_hash, last_reply_at,
                        talk_value_override, cooldown_override, force_silent
                    ) VALUES (?, ?, 1, 0, 0, 0, 1.0, 'idle', '', '', 0, -1.0, -1, 0)
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
                    is_mentioned, is_command_like, is_low_signal, content_text, raw_summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.unified_msg_origin,
                    message.sender_id,
                    message.sender_name,
                    int(message.is_bot),
                    int(message.is_mentioned),
                    int(message.is_command_like),
                    int(message.is_low_signal),
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

    async def has_recent_duplicate_message(self, message: NormalizedMessage, within_seconds: int = 30) -> bool:
        return await asyncio.to_thread(self._has_recent_duplicate_message_sync, message, within_seconds)

    def _has_recent_duplicate_message_sync(self, message: NormalizedMessage, within_seconds: int) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM recent_messages
                WHERE unified_msg_origin = ?
                  AND sender_id = ?
                  AND is_bot = 0
                  AND raw_summary = ?
                  AND created_at >= ?
                """,
                (
                    message.unified_msg_origin,
                    message.sender_id,
                    message.raw_summary,
                    message.created_at - within_seconds,
                ),
            ).fetchone()
        return bool(row and int(row["cnt"]) > 0)

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

    async def get_group_pacing_stats(
        self,
        origin: str,
        since_read: float,
        activity_window_start: float,
    ) -> GroupPacingStats:
        return await asyncio.to_thread(self._get_group_pacing_stats_sync, origin, since_read, activity_window_start)

    def _get_group_pacing_stats_sync(
        self,
        origin: str,
        since_read: float,
        activity_window_start: float,
    ) -> GroupPacingStats:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(
                        CASE
                            WHEN is_bot = 0
                             AND is_command_like = 0
                             AND is_low_signal = 0
                             AND created_at > ?
                            THEN 1 ELSE 0
                        END
                    ) AS unread_count,
                    SUM(
                        CASE
                            WHEN is_bot = 0
                             AND is_command_like = 0
                             AND is_low_signal = 0
                             AND created_at >= ?
                            THEN 1 ELSE 0
                        END
                    ) AS activity_count,
                    MAX(
                        CASE
                            WHEN is_bot = 0
                             AND is_command_like = 0
                             AND is_low_signal = 0
                            THEN created_at ELSE NULL
                        END
                    ) AS latest_human_message_at
                FROM recent_messages
                WHERE unified_msg_origin = ?
                """,
                (since_read, activity_window_start, origin),
            ).fetchone()
        return GroupPacingStats(
            unread_human_messages=int(row["unread_count"] or 0) if row else 0,
            recent_activity_messages=int(row["activity_count"] or 0) if row else 0,
            latest_human_message_at=float(row["latest_human_message_at"] or 0.0) if row else 0.0,
        )

    async def count_unread_human_messages(self, origin: str, since: float) -> int:
        stats = await self.get_group_pacing_stats(origin, since_read=since, activity_window_start=0.0)
        return stats.unread_human_messages

    async def add_action_record(self, origin: str, plan: ActionPlan, created_at: float | None = None) -> None:
        await asyncio.to_thread(self._add_action_record_sync, origin, plan, created_at or time.time())

    def _add_action_record_sync(self, origin: str, plan: ActionPlan, created_at: float) -> None:
        payload_summary = self._build_payload_summary(plan)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO action_records (
                    unified_msg_origin, action, reason, target_message_id, payload_summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (origin, plan.action, plan.reason, plan.target_message_id, payload_summary, created_at),
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

    def _build_payload_summary(self, plan: ActionPlan) -> str:
        parts: list[str] = []
        if plan.question:
            parts.append(f"question={plan.question}")
        if plan.unknown_words:
            parts.append(f"unknown={','.join(plan.unknown_words[:3])}")
        if plan.quote:
            parts.append("quote=true")
        if plan.wait_seconds:
            parts.append(f"wait={plan.wait_seconds}")
        return "; ".join(parts)

    async def get_recent_actions(self, origin: str, limit: int = 5) -> list[ActionRecord]:
        return await asyncio.to_thread(self._get_recent_actions_sync, origin, limit)

    def _get_recent_actions_sync(self, origin: str, limit: int) -> list[ActionRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT action, reason, target_message_id, payload_summary, created_at
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
                payload_summary=str(row["payload_summary"] or ""),
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

    async def mark_reply_sent(
        self,
        origin: str,
        sent_at: float | None = None,
        target_message_id: str = "",
        reply_text_hash: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._mark_reply_sent_sync,
            origin,
            sent_at or time.time(),
            target_message_id,
            reply_text_hash,
        )

    def _mark_reply_sent_sync(
        self,
        origin: str,
        sent_at: float,
        target_message_id: str,
        reply_text_hash: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_sessions
                SET last_active_at = ?,
                    consecutive_no_reply_count = 0,
                    state = 'idle',
                    last_reply_target_message_id = ?,
                    last_reply_text_hash = ?,
                    last_reply_at = ?
                WHERE unified_msg_origin = ?
                """,
                (sent_at, target_message_id, reply_text_hash, sent_at, origin),
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

    async def reset_consecutive_no_reply(self, origin: str) -> None:
        await asyncio.to_thread(self._reset_consecutive_no_reply_sync, origin)

    def _reset_consecutive_no_reply_sync(self, origin: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE chat_sessions SET consecutive_no_reply_count = 0 WHERE unified_msg_origin = ?",
                (origin,),
            )

    async def adjust_talk_frequency(
        self,
        origin: str,
        delta: float,
        minimum: float,
        maximum: float,
    ) -> float:
        return await asyncio.to_thread(self._adjust_talk_frequency_sync, origin, delta, minimum, maximum)

    def _adjust_talk_frequency_sync(self, origin: str, delta: float, minimum: float, maximum: float) -> float:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT talk_frequency_adjust FROM chat_sessions WHERE unified_msg_origin = ?",
                (origin,),
            ).fetchone()
            current = float(row["talk_frequency_adjust"]) if row else 1.0
            updated = clamp_talk_frequency_adjust(current + delta, minimum=minimum, maximum=maximum)
            conn.execute(
                "UPDATE chat_sessions SET talk_frequency_adjust = ? WHERE unified_msg_origin = ?",
                (updated, origin),
            )
        return updated

    async def recover_group_pacing(
        self,
        origin: str,
        now: float,
        recovery_after_seconds: int,
        minimum: float,
        maximum: float,
    ) -> SessionRecord:
        return await asyncio.to_thread(
            self._recover_group_pacing_sync,
            origin,
            now,
            recovery_after_seconds,
            minimum,
            maximum,
        )

    def _recover_group_pacing_sync(
        self,
        origin: str,
        now: float,
        recovery_after_seconds: int,
        minimum: float,
        maximum: float,
    ) -> SessionRecord:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE unified_msg_origin = ?",
                (origin,),
            ).fetchone()
            if row is None:
                raise ValueError(f"session-not-found: {origin}")

            last_active_at = float(row["last_active_at"] or 0.0)
            last_read_at = float(row["last_read_at"] or 0.0)
            talk_frequency_adjust = float(row["talk_frequency_adjust"] or 1.0)
            new_no_reply_count = int(row["consecutive_no_reply_count"] or 0)

            last_signal_at = max(last_active_at, last_read_at)
            if last_signal_at and (now - last_signal_at) >= recovery_after_seconds:
                new_no_reply_count = 0

            updated_frequency = talk_frequency_adjust
            if last_active_at and (now - last_active_at) >= recovery_after_seconds and talk_frequency_adjust < 1.0:
                updated_frequency = min(
                    1.0,
                    clamp_talk_frequency_adjust(
                        talk_frequency_adjust + RECOVERY_STEP,
                        minimum=minimum,
                        maximum=maximum,
                    ),
                )

            conn.execute(
                """
                UPDATE chat_sessions
                SET consecutive_no_reply_count = ?,
                    talk_frequency_adjust = ?
                WHERE unified_msg_origin = ?
                """,
                (new_no_reply_count, updated_frequency, origin),
            )
            updated_row = conn.execute(
                "SELECT * FROM chat_sessions WHERE unified_msg_origin = ?",
                (origin,),
            ).fetchone()
        return self._row_to_session(updated_row)  # type: ignore[arg-type]

    async def is_duplicate_reply(
        self,
        origin: str,
        target_message_id: str,
        reply_text_hash: str,
        now: float,
        within_seconds: int,
    ) -> bool:
        return await asyncio.to_thread(
            self._is_duplicate_reply_sync,
            origin,
            target_message_id,
            reply_text_hash,
            now,
            within_seconds,
        )

    def _is_duplicate_reply_sync(
        self,
        origin: str,
        target_message_id: str,
        reply_text_hash: str,
        now: float,
        within_seconds: int,
    ) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_reply_target_message_id, last_reply_text_hash, last_reply_at
                FROM chat_sessions
                WHERE unified_msg_origin = ?
                """,
                (origin,),
            ).fetchone()
        if row is None:
            return False
        last_reply_at = float(row["last_reply_at"] or 0)
        if not last_reply_at or (now - last_reply_at) > within_seconds:
            return False
        if target_message_id and str(row["last_reply_target_message_id"] or "") == target_message_id:
            return True
        return bool(reply_text_hash and str(row["last_reply_text_hash"] or "") == reply_text_hash)

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
            last_reply_target_message_id=str(row["last_reply_target_message_id"] or ""),
            last_reply_text_hash=str(row["last_reply_text_hash"] or ""),
            last_reply_at=float(row["last_reply_at"] or 0),
            talk_value_override=float(row["talk_value_override"] or -1.0),
            cooldown_override=int(row["cooldown_override"] or -1),
            force_silent=bool(row["force_silent"]),
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
            is_low_signal=bool(row["is_low_signal"]),
        )
