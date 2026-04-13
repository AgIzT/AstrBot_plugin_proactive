from __future__ import annotations

import asyncio
import random
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.agent.message import AssistantMessageSegment, TextPart, UserMessageSegment

from .config import PluginConfig
from .models import NormalizedMessage
from .planner import PlannerEngine
from .policy import compute_group_trigger, should_ignore_message, should_observe_private
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

        session = await self.store.upsert_session(message)
        await self.store.save_message(message, self.config.max_context_messages)

        if message.chat_type == "private" and message.unified_msg_origin in self._wait_tasks:
            self._wait_tasks.pop(message.unified_msg_origin).cancel()
            await self.store.clear_waiting(message.unified_msg_origin)

        if message.chat_type == "group":
            unread_count = await self.store.count_unread_human_messages(
                message.unified_msg_origin,
                session.last_read_at,
            )
            decision = compute_group_trigger(
                message=message,
                session=session,
                unread_human_messages=unread_count,
                config=self.config,
                random_value=random.random(),
                now=time.time(),
            )
            if decision.should_observe:
                await self._schedule_observation(message.unified_msg_origin, "group", decision.reason)
        else:
            decision = should_observe_private(message, self.config)
            if decision.should_observe:
                await self._schedule_observation(message.unified_msg_origin, "private", decision.reason)

    async def _schedule_observation(self, origin: str, chat_type: str, trigger_reason: str) -> None:
        lock = self._observe_locks.setdefault(origin, asyncio.Lock())
        if lock.locked():
            self._pending_reobserve.add(origin)
            return
        asyncio.create_task(self._observe(origin, chat_type, trigger_reason))

    async def _observe(self, origin: str, chat_type: str, trigger_reason: str) -> None:
        lock = self._observe_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            await self.store.mark_observed(origin, time.time())
            messages = await self.store.get_recent_messages(origin, self.config.max_context_messages)
            if not messages:
                return

            provider_id = await self._get_provider_id(origin)
            if not provider_id:
                logger.warning("No provider available for proactive reply: %s", origin)
                if chat_type == "group":
                    await self.store.increment_no_reply(origin)
                else:
                    await self.store.clear_waiting(origin)
                return

            recent_actions = await self.store.get_recent_actions(origin, limit=5)
            action_lines = [
                f"{record.action} | {record.reason} | {record.target_message_id}"
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

            if plan.action == "reply":
                reply_text = await self._try_generate_reply(origin, chat_type, messages, plan, provider_id)
                if reply_text:
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
                    await self.store.mark_reply_sent(origin, sent_at)
                    if self.config.write_back_to_conversation:
                        await self._write_back_to_conversation(origin, messages[-1], reply_text)
                else:
                    if chat_type == "group":
                        await self.store.increment_no_reply(origin)
                    else:
                        await self.store.set_waiting(origin, time.time() + self.config.private_wait_default_seconds)
                        self._arm_wait(origin, self.config.private_wait_default_seconds)
            elif plan.action == "wait":
                wait_seconds = plan.wait_seconds or self.config.private_wait_default_seconds
                await self.store.set_waiting(origin, time.time() + wait_seconds)
                self._arm_wait(origin, wait_seconds)
            elif plan.action == "complete_talk":
                await self.store.clear_waiting(origin)
            else:
                await self.store.increment_no_reply(origin)

        if origin in self._pending_reobserve:
            self._pending_reobserve.discard(origin)
            asyncio.create_task(self._observe(origin, chat_type, "pending"))

    def _arm_wait(self, origin: str, wait_seconds: int) -> None:
        if origin in self._wait_tasks:
            self._wait_tasks.pop(origin).cancel()
        self._wait_tasks[origin] = asyncio.create_task(self._wait_and_reobserve(origin, wait_seconds))

    async def _wait_and_reobserve(self, origin: str, wait_seconds: int) -> None:
        try:
            await asyncio.sleep(wait_seconds)
            session = await self.store.get_session(origin)
            if session.chat_type == "private":
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
        plan: Any,
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
        is_command_like = summary.strip().startswith(("/", "!", ".", "#"))

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
            is_bot=is_bot,
            is_mentioned=is_mentioned,
            is_command_like=is_command_like,
        )
        if should_ignore_message(normalized, self.config) and not normalized.is_mentioned:
            return normalized
        return normalized
