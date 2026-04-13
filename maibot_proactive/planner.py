from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, Iterable

from .config import PluginConfig
from .models import ActionPlan, NormalizedMessage

LLMCaller = Callable[[str], Awaitable[str]]


class PlannerEngine:
    def __init__(self, config: PluginConfig):
        self.config = config

    async def plan(
        self,
        chat_type: str,
        messages: list[NormalizedMessage],
        actions_before_now: list[str],
        trigger_reason: str,
        llm_call: LLMCaller,
    ) -> ActionPlan:
        prompt = self._build_prompt(chat_type, messages, actions_before_now, trigger_reason)
        try:
            raw_output = await llm_call(prompt)
        except Exception:
            return self._fallback_plan(chat_type, messages, "planner-call-failed")
        return self._parse_output(raw_output, chat_type, messages)

    def _build_prompt(
        self,
        chat_type: str,
        messages: list[NormalizedMessage],
        actions_before_now: list[str],
        trigger_reason: str,
    ) -> str:
        conversation = "\n".join(
            f"{msg.message_id} | {'bot' if msg.is_bot else msg.sender_name}: {msg.raw_summary}"
            for msg in messages[-self.config.max_context_messages :]
        )
        history = "\n".join(actions_before_now[-5:]) or "none"
        actions_text = (
            "Allowed action values:\n1. reply\n2. no_reply\n"
            if chat_type == "group"
            else "Allowed action values:\n1. reply\n2. wait\n3. complete_talk\n"
        )

        return (
            "You are a cautious proactive reply planner inside AstrBot.\n"
            f"Current chat type: {'group' if chat_type == 'group' else 'private'}\n"
            f"Trigger reason: {trigger_reason}\n"
            f"Recent action history:\n{history}\n\n"
            f"Recent chat messages:\n{conversation}\n\n"
            f"{actions_text}\n"
            "Rules:\n"
            "- If this is not a good time to speak, prefer no_reply in groups, or wait/complete_talk in private chat.\n"
            "- Never reply to the bot's own message.\n"
            "- Group replies must be brief and non-disruptive.\n"
            "- When choosing reply, you may also provide unknown_words, question, and quote.\n"
            "- Output exactly one JSON object and nothing else.\n"
            "JSON schema:\n"
            "{\"action\":\"reply|no_reply|wait|complete_talk\",\"target_message_id\":\"message-id\",\"reason\":\"short reason\",\"unknown_words\":[\"term\"],\"question\":\"search hint\",\"quote\":false,\"wait_seconds\":5}"
        )

    def _parse_output(self, raw_output: str, chat_type: str, messages: list[NormalizedMessage]) -> ActionPlan:
        try:
            payload = self._extract_json(raw_output)
            action = str(payload.get("action", "") or "").strip().lower()
            allowed = {"reply", "no_reply"} if chat_type == "group" else {"reply", "wait", "complete_talk"}
            if action not in allowed:
                return self._fallback_plan(chat_type, messages, "invalid-action")

            target_message_id = str(payload.get("target_message_id", "") or "").strip()
            if not target_message_id and messages:
                target_message_id = messages[-1].message_id

            target_message = next((msg for msg in messages if msg.message_id == target_message_id), None)
            if action == "reply" and target_message and target_message.is_bot:
                return self._fallback_plan(chat_type, messages, "self-target")

            reason = str(payload.get("reason", "") or "").strip() or "planner"
            unknown_words = _clean_string_list(payload.get("unknown_words"))
            question = str(payload.get("question", "") or "").strip()
            quote = bool(payload.get("quote", False))
            wait_seconds = _normalize_wait_seconds(payload.get("wait_seconds", 0), self.config.private_wait_default_seconds)

            if action == "wait":
                return ActionPlan(
                    action="wait",
                    target_message_id=target_message_id,
                    reason=reason,
                    wait_seconds=wait_seconds,
                )

            return ActionPlan(
                action=action,  # type: ignore[arg-type]
                target_message_id=target_message_id,
                reason=reason,
                unknown_words=unknown_words,
                question=question,
                quote=quote,
            )
        except Exception:
            return self._fallback_plan(chat_type, messages, "parse-failed")

    def _fallback_plan(self, chat_type: str, messages: list[NormalizedMessage], reason: str) -> ActionPlan:
        if chat_type == "private":
            return ActionPlan(
                action="wait",
                target_message_id=messages[-1].message_id if messages else "",
                reason=reason,
                wait_seconds=self.config.private_wait_default_seconds,
            )
        return ActionPlan(
            action="no_reply",
            target_message_id=messages[-1].message_id if messages else "",
            reason=reason,
        )

    def _extract_json(self, raw_output: str) -> dict:
        fenced_match = re.search(r"```json\s*(\{.*?\})\s*```", raw_output, re.DOTALL)
        if fenced_match:
            return json.loads(fenced_match.group(1))
        start = raw_output.find("{")
        end = raw_output.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("json-not-found")
        return json.loads(raw_output[start : end + 1])


def _clean_string_list(raw_value: object) -> list[str]:
    if not isinstance(raw_value, Iterable) or isinstance(raw_value, (str, bytes)):
        return []
    results: list[str] = []
    for item in raw_value:
        text = str(item).strip()
        if text:
            results.append(text)
    return results


def _normalize_wait_seconds(raw_value: object, default_value: int) -> int:
    try:
        wait_seconds = int(float(raw_value))
        if wait_seconds <= 0:
            return default_value
        return wait_seconds
    except (TypeError, ValueError):
        return default_value
