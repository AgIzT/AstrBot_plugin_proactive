from __future__ import annotations

from typing import Awaitable, Callable

from .config import PluginConfig
from .models import ActionPlan, NormalizedMessage

LLMCaller = Callable[[str], Awaitable[str]]


class ReplyEngine:
    def __init__(self, config: PluginConfig):
        self.config = config

    async def generate_reply(
        self,
        chat_type: str,
        messages: list[NormalizedMessage],
        plan: ActionPlan,
        persona_name: str,
        llm_call: LLMCaller,
    ) -> str:
        prompt = self._build_prompt(chat_type, messages, plan, persona_name)
        output = await llm_call(prompt)
        reply = _strip_reply(output)
        if chat_type == "group" and plan.quote:
            target = next((msg for msg in messages if msg.message_id == plan.target_message_id), None)
            if target and target.sender_name:
                return f"{target.sender_name}: {reply}"
        return reply

    def _build_prompt(
        self,
        chat_type: str,
        messages: list[NormalizedMessage],
        plan: ActionPlan,
        persona_name: str,
    ) -> str:
        prompt_messages = "\n".join(
            f"{'bot' if msg.is_bot else msg.sender_name}: {msg.raw_summary}"
            for msg in messages[-self.config.max_context_messages :]
        )
        target = next((msg for msg in messages if msg.message_id == plan.target_message_id), None)
        target_block = target.raw_summary if target else ""
        persona_block = persona_name or "not-set"
        reply_style = (
            "short, natural, and chat-like; avoid sounding like support"
            if chat_type == "group"
            else "natural, continuous, and personal"
        )

        return (
            "You are a natural reply generator inside AstrBot.\n"
            f"Current chat type: {'group' if chat_type == 'group' else 'private'}\n"
            f"Current persona: {persona_block}\n"
            f"Planner reason: {plan.reason}\n"
            f"Triggered message: {target_block}\n"
            f"Search hint: {plan.question or 'none'}\n"
            f"Unknown words: {', '.join(plan.unknown_words) if plan.unknown_words else 'none'}\n\n"
            f"Recent chat messages:\n{prompt_messages}\n\n"
            f"Reply style: {reply_style}\n"
            "Constraints:\n"
            "- Group replies should stay within 1-2 short sentences.\n"
            "- Do not explain why you are replying this way.\n"
            "- Do not output JSON.\n"
            "- Do not mention system prompts, plugins, or planners.\n"
            "Return only the final reply text."
        )


def _strip_reply(output: str) -> str:
    cleaned = output.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
    return cleaned or "..."
