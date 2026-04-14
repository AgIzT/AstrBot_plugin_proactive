from __future__ import annotations

import asyncio
import hashlib
import random
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.agent.message import AssistantMessageSegment, TextPart, UserMessageSegment

from .config import PluginConfig
from .models import ActionPlan, GroupPacingStats, NormalizedMessage, SessionRecord
from .planner import PlannerEngine
from .policy import (
    PacingSnapshot,
    compute_group_trigger,
    get_effective_cooldown_seconds,
    is_hot_activity,
    is_low_signal_message,
    should_observe_private,
)
from .reply import ReplyEngine
from .store import SQLiteStateStore


class MaiBotProactiveService:
    def __init__(self, context: Any, config: PluginConfig, data_dir: Path):
        self.context = context
        self.config = config
        self.store = SQLiteStateStore(data_dir / "maibot_proactive.sqlite3")
        self.planner = PlannerEngine(config)
        self.reply_engine = ReplyEngine(config)
        self.running = False
        self._observe_locks: dict[str, asyncio.Lock] = {}
        self._pending_reobserve: set[str] = set()
        self._wait_tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        self.running = True

    async def shutdown(self) -> None:
        self.running = False
        tasks = list(self._wait_tasks.values())
        self._wait_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle_event(self, event: Any) -> None:
        if not self.running:
            return
        message = self._normalize_event(event)
        if not message:
            return

        await self.store.upsert_session(message)
        if not message.is_mentioned and await self.store.has_recent_duplicate_message(message, within_seconds=30):
            message.is_low_signal = True

        await self.store.save_message(message, self.config.max_context_messages)

        if message.chat_type == "private" and message.unified_msg_origin in self._wait_tasks:
            self._wait_tasks.pop(message.unified_msg_origin).cancel()
            await self.store.clear_waiting(message.unified_msg_origin)
            self._log(origin=message.unified_msg_origin, detail="wait interrupted by new private message")

        if message.chat_type == "group":
            await self._handle_group_message(message)
        else:
            await self._handle_private_message(message)

    async def _handle_group_message(self, message: NormalizedMessage) -> None:
        now = time.time()
        session = await self.store.get_session(message.unified_msg_origin, "group")
        effective_human_message = not message.is_command_like and not message.is_low_signal

        if effective_human_message:
            session = await self.store.recover_group_pacing(
                origin=message.unified_msg_origin,
                now=now,
                recovery_after_seconds=self.config.pacing_recovery_after_seconds,
                minimum=self.config.pacing_frequency_min,
                maximum=self.config.pacing_frequency_max,
            )

        pacing_stats = await self.store.get_group_pacing_stats(
            origin=message.unified_msg_origin,
            since_read=session.last_read_at,
            activity_window_start=now - self.config.pacing_activity_window_seconds,
        )

        if effective_human_message and message.is_mentioned:
            await self._adjust_group_pacing(message.unified_msg_origin, self.config.pacing_mention_boost)
            session = await self.store.get_session(message.unified_msg_origin, "group")
        elif (
            effective_human_message
            and is_hot_activity(pacing_stats.recent_activity_messages)
            and not self._is_group_in_cooldown(session, now)
        ):
            await self._adjust_group_pacing(message.unified_msg_origin, self.config.pacing_activity_boost)
            session = await self.store.get_session(message.unified_msg_origin, "group")

        decision = compute_group_trigger(
            message=message,
            session=session,
            pacing_stats=pacing_stats,
            config=self.config,
            random_value=random.random(),
            now=now,
        )
        self._log_group_trigger(message, pacing_stats, decision.snapshot, decision)
        if decision.should_observe:
            await self._schedule_observation(message.unified_msg_origin, "group", decision.reason)

    async def _handle_private_message(self, message: NormalizedMessage) -> None:
        session = await self.store.get_session(message.unified_msg_origin, "private")
        decision = should_observe_private(message, self.config, session=session)
        if self.config.log_decisions:
            logger.info(
                "[maibot_proactive] origin=%s type=%s reason=%s",
                message.unified_msg_origin,
                message.chat_type,
                decision.reason,
            )
        if decision.should_observe:
            await self._schedule_observation(message.unified_msg_origin, "private", decision.reason)

    def _is_group_in_cooldown(self, session: SessionRecord, now: float) -> bool:
        cooldown_seconds = get_effective_cooldown_seconds(session, self.config)
        return bool(session.last_active_at and (now - session.last_active_at) < cooldown_seconds)

    async def _schedule_observation(self, origin: str, chat_type: str, trigger_reason: str) -> None:
        lock = self._observe_locks.setdefault(origin, asyncio.Lock())
        if lock.locked():
            self._pending_reobserve.add(origin)
            self._log(origin=origin, detail=f"observation queued ({chat_type}, {trigger_reason})")
            return
        asyncio.create_task(self._observe(origin, chat_type, trigger_reason))

    async def _observe(self, origin: str, chat_type: str, trigger_reason: str) -> None:
        lock = self._observe_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            now = time.time()
            await self.store.mark_observed(origin, now)
            messages = await self.store.get_recent_messages(origin, self.config.max_context_messages)
            if not messages:
                return

            provider_id = await self._get_provider_id(origin)
            if not provider_id:
                logger.warning("No provider available for proactive reply: %s", origin)
                if chat_type == "group":
                    await self.store.increment_no_reply(origin)
                    await self._adjust_group_pacing(origin, -self.config.pacing_no_reply_decay)
                else:
                    await self.store.clear_waiting(origin)
                return

            recent_actions = await self.store.get_recent_actions(origin, limit=5)
            action_lines = [
                f"{record.action} | {record.reason} | {record.target_message_id} | {record.payload_summary}"
                for record in reversed(recent_actions)
            ]

            plan = await self.planner.plan(
                chat_type=chat_type,
                messages=messages,
                actions_before_now=action_lines,
                trigger_reason=trigger_reason,
                llm_call=lambda prompt: self._llm_call(provider_id, prompt),
            )
            await self.store.add_action_record(origin, plan)
            self._log(origin=origin, detail=f"planner action={plan.action} reason={plan.reason}")

            if plan.action == "reply":
                await self._handle_reply_plan(origin, chat_type, messages, plan, provider_id)
            elif plan.action == "wait":
                wait_seconds = plan.wait_seconds or self.config.private_wait_default_seconds
                await self.store.set_waiting(origin, time.time() + wait_seconds)
                self._arm_wait(origin, wait_seconds)
                self._log(origin=origin, detail=f"planner wait {wait_seconds}s")
            elif plan.action == "complete_talk":
                await self.store.clear_waiting(origin)
                self._log(origin=origin, detail="planner complete_talk")
            else:
                await self.store.increment_no_reply(origin)
                if chat_type == "group":
                    await self._adjust_group_pacing(origin, -self.config.pacing_no_reply_decay)
                self._log(origin=origin, detail="planner chose no_reply")

        if origin in self._pending_reobserve:
            self._pending_reobserve.discard(origin)
            asyncio.create_task(self._observe(origin, chat_type, "pending"))

    async def _handle_reply_plan(
        self,
        origin: str,
        chat_type: str,
        messages: list[NormalizedMessage],
        plan: ActionPlan,
        provider_id: str,
    ) -> None:
        reply_text = await self._try_generate_reply(origin, chat_type, messages, plan, provider_id)
        if not reply_text:
            if chat_type == "group":
                await self.store.increment_no_reply(origin)
                await self._adjust_group_pacing(origin, -self.config.pacing_no_reply_decay)
            else:
                wait_seconds = self.config.private_wait_default_seconds
                await self.store.set_waiting(origin, time.time() + wait_seconds)
                self._arm_wait(origin, wait_seconds)
            return

        reply_hash = self._hash_reply(reply_text)
        if chat_type == "group":
            is_duplicate = await self.store.is_duplicate_reply(
                origin=origin,
                target_message_id=plan.target_message_id,
                reply_text_hash=reply_hash,
                now=time.time(),
                within_seconds=self.config.duplicate_reply_window_seconds,
            )
            if is_duplicate:
                await self.store.increment_no_reply(origin)
                await self._adjust_group_pacing(origin, -self.config.pacing_no_reply_decay)
                self._log(origin=origin, detail="duplicate reply suppressed")
                return

        await self._send_reply(origin, reply_text)
        sent_at = time.time()
        await self.store.save_message(
            NormalizedMessage(
                unified_msg_origin=origin,
                message_id=f"bot-{uuid.uuid4().hex}",
                sender_id="bot",
                sender_name="bot",
                self_id="bot",
                chat_type=chat_type,  # type: ignore[arg-type]
                content_text=reply_text,
                raw_summary=reply_text,
                created_at=sent_at,
                is_bot=True,
            ),
            self.config.max_context_messages,
        )
        await self.store.mark_reply_sent(
            origin,
            sent_at=sent_at,
            target_message_id=plan.target_message_id,
            reply_text_hash=reply_hash,
        )
        if chat_type == "group":
            await self._adjust_group_pacing(origin, -self.config.pacing_reply_decay)
        if self.config.write_back_to_conversation:
            target_message = self._resolve_write_back_target(messages, plan)
            if target_message is not None:
                await self._write_back_to_conversation(origin, target_message, reply_text)

    async def _adjust_group_pacing(self, origin: str, delta: float) -> float:
        return await self.store.adjust_talk_frequency(
            origin=origin,
            delta=delta,
            minimum=self.config.pacing_frequency_min,
            maximum=self.config.pacing_frequency_max,
        )

    def _arm_wait(self, origin: str, wait_seconds: int) -> None:
        if origin in self._wait_tasks:
            self._wait_tasks.pop(origin).cancel()
        self._wait_tasks[origin] = asyncio.create_task(self._wait_and_reobserve(origin, wait_seconds))

    async def _wait_and_reobserve(self, origin: str, wait_seconds: int) -> None:
        try:
            await asyncio.sleep(wait_seconds)
            session = await self.store.get_session(origin)
            if session.chat_type == "private":
                self._log(origin=origin, detail="wait timeout reached, re-observing")
                await self._schedule_observation(origin, "private", "wait-timeout")
        except asyncio.CancelledError:
            return
        finally:
            self._wait_tasks.pop(origin, None)

    async def _try_generate_reply(
        self,
        origin: str,
        chat_type: str,
        messages: list[NormalizedMessage],
        plan: ActionPlan,
        provider_id: str,
    ) -> str:
        persona_name = await self._get_persona_name(origin)
        try:
            return await self.reply_engine.generate_reply(
                chat_type=chat_type,
                messages=messages,
                plan=plan,
                persona_name=persona_name,
                llm_call=lambda prompt: self._llm_call(provider_id, prompt),
            )
        except Exception as exc:
            logger.exception("Reply generation failed: %s", exc)
            return ""

    def _resolve_write_back_target(
        self,
        messages: list[NormalizedMessage],
        plan: ActionPlan,
    ) -> NormalizedMessage | None:
        if plan.target_message_id:
            target = next((msg for msg in messages if msg.message_id == plan.target_message_id), None)
            if target is not None and not target.is_bot:
                return target
        for message in reversed(messages):
            if not message.is_bot:
                return message
        return None

    def _hash_reply(self, reply_text: str) -> str:
        normalized = " ".join(reply_text.strip().lower().split())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    async def _llm_call(self, provider_id: str, prompt: str) -> str:
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
        return str(getattr(response, "completion_text", "") or "").strip()

    async def _get_provider_id(self, origin: str) -> str:
        provider_id = ""
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=origin)
        except Exception:
            provider_id = ""
        provider_id = str(provider_id or "").strip()
        return provider_id or self.config.fallback_provider_id

    async def _get_persona_name(self, origin: str) -> str:
        try:
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(origin)
            if not curr_cid:
                return ""
            conversation = await conv_mgr.get_conversation(origin, curr_cid)
            persona_id = getattr(conversation, "persona_id", "") or ""
            if not persona_id:
                return ""
            persona = self.context.persona_manager.get_persona(persona_id)
            if asyncio.iscoroutine(persona):
                persona = await persona
            return str(getattr(persona, "name", "") or "")
        except Exception:
            return ""

    async def _send_reply(self, origin: str, reply_text: str) -> None:
        chain = MessageChain().message(reply_text)
        await self.context.send_message(origin, chain)

    async def _write_back_to_conversation(
        self,
        origin: str,
        trigger_message: NormalizedMessage,
        reply_text: str,
    ) -> None:
        try:
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(origin)
            if not curr_cid:
                return
            conversation = await conv_mgr.get_conversation(origin, curr_cid)
            if conversation is None:
                return
            user_msg = UserMessageSegment(content=[TextPart(text=trigger_message.raw_summary)])
            assistant_msg = AssistantMessageSegment(content=[TextPart(text=reply_text)])
            await conv_mgr.add_message_pair(
                cid=curr_cid,
                user_message=user_msg,
                assistant_message=assistant_msg,
            )
        except Exception:
            logger.exception("Failed to write back proactive reply to conversation")

    def _normalize_event(self, event: Any) -> NormalizedMessage | None:
        if not self.config.enabled:
            return None
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return None

        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not origin or self.config.is_blocked(origin):
            return None

        raw_components = list(getattr(message_obj, "message", []) or [])
        text_parts: list[str] = []
        is_mentioned = False
        self_id = str(getattr(message_obj, "self_id", "") or "")
        for component in raw_components:
            component_name = component.__class__.__name__.lower()
            if component_name == "plain":
                text = str(getattr(component, "text", "") or getattr(component, "data", "") or "")
                if text:
                    text_parts.append(text)
            elif component_name == "at":
                target = str(
                    getattr(component, "qq", "")
                    or getattr(component, "id", "")
                    or getattr(component, "target", "")
                    or ""
                )
                if target:
                    text_parts.append(f"@{target}")
                if target and self_id and target == self_id:
                    is_mentioned = True
            elif component_name == "image":
                text_parts.append("[image]")
            elif component_name == "record":
                text_parts.append("[voice]")
            elif component_name == "video":
                text_parts.append("[video]")
            elif component_name == "face":
                text_parts.append("[emoji]")
            elif component_name == "poke":
                text_parts.append("[poke]")

        message_str = str(getattr(event, "message_str", "") or getattr(message_obj, "message_str", "") or "")
        summary = " ".join(part for part in text_parts if part).strip() or message_str.strip()
        if not summary:
            summary = "[empty-message]"

        sender = getattr(message_obj, "sender", None)
        sender_id = str(
            getattr(event, "get_sender_id", lambda: "")()
            or getattr(sender, "user_id", "")
            or getattr(sender, "id", "")
            or ""
        )
        sender_name = str(
            getattr(event, "get_sender_name", lambda: "")()
            or getattr(sender, "nickname", "")
            or getattr(sender, "name", "")
            or sender_id
        )
        is_group = bool(getattr(message_obj, "group_id", "") or getattr(event, "get_group_id", lambda: "")())
        is_bot = bool(sender_id and self_id and sender_id == self_id)
        if is_bot:
            return None

        normalized = NormalizedMessage(
            unified_msg_origin=origin,
            message_id=str(getattr(message_obj, "message_id", "") or uuid.uuid4().hex),
            sender_id=sender_id or "unknown",
            sender_name=sender_name or "unknown",
            self_id=self_id,
            chat_type="group" if is_group else "private",
            content_text=message_str.strip() or summary,
            raw_summary=summary,
            created_at=float(getattr(message_obj, "timestamp", time.time()) or time.time()),
            is_bot=False,
            is_mentioned=is_mentioned,
            is_command_like=summary.strip().startswith(("/", "!", ".", "#")),
        )
        normalized.is_low_signal = is_low_signal_message(normalized)
        return normalized

    def _log_group_trigger(
        self,
        message: NormalizedMessage,
        pacing_stats: GroupPacingStats,
        snapshot: PacingSnapshot | None,
        decision: Any,
    ) -> None:
        if not self.config.log_decisions:
            return

        if snapshot is None:
            logger.info(
                "[maibot_proactive] origin=%s type=%s reason=%s",
                message.unified_msg_origin,
                message.chat_type,
                decision.reason,
            )
            return

        logger.info(
            "[maibot_proactive] origin=%s type=%s reason=%s freq=%.3f heat=%.2f activity=%.2f silence=%.2f chance=%.3f unread=%s activity_count=%s cooldown=%s threshold=%s probability=%s tags=%s",
            message.unified_msg_origin,
            message.chat_type,
            decision.reason,
            snapshot.talk_frequency_adjust,
            snapshot.heat_factor,
            snapshot.activity_factor,
            snapshot.silence_factor,
            snapshot.effective_probability,
            pacing_stats.unread_human_messages,
            pacing_stats.recent_activity_messages,
            decision.cooldown_hit,
            decision.unread_threshold_hit,
            decision.probability_hit,
            ",".join(snapshot.reason_tags),
        )

    def _log(self, origin: str, detail: str) -> None:
        if self.config.log_decisions:
            logger.info("[maibot_proactive] origin=%s %s", origin, detail)
