import sys
import tempfile
import types
import unittest
from pathlib import Path


class DummyLogger:
    def __init__(self):
        self.records = []

    def info(self, *args, **kwargs):
        self.records.append(("info", args, kwargs))

    def error(self, *args, **kwargs):
        self.records.append(("error", args, kwargs))

    def exception(self, *args, **kwargs):
        self.records.append(("exception", args, kwargs))

    def warning(self, *args, **kwargs):
        self.records.append(("warning", args, kwargs))


class DummyMessageChain:
    def __init__(self):
        self.parts = []

    def message(self, text):
        self.parts.append(text)
        return self


class DummySegment:
    def __init__(self, content):
        self.content = content


def install_astrbot_stubs():
    logger = DummyLogger()
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logger
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = DummyMessageChain
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    message = types.ModuleType("astrbot.core.agent.message")
    message.AssistantMessageSegment = DummySegment
    message.UserMessageSegment = DummySegment
    message.TextPart = DummySegment

    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)
    sys.modules.setdefault("astrbot.api.event", event)
    sys.modules.setdefault("astrbot.core", core)
    sys.modules.setdefault("astrbot.core.agent", agent)
    sys.modules.setdefault("astrbot.core.agent.message", message)
    return logger


LOGGER = install_astrbot_stubs()

from maibot_proactive.config import PluginConfig
from maibot_proactive.runtime import MaiBotProactiveService


class DummyConfig(dict):
    def save_config(self):
        return None


class DummyContext:
    def __init__(self, send_result=True, send_error=None):
        self.send_result = send_result
        self.send_error = send_error
        self.sent = []

    async def send_message(self, origin, chain):
        if self.send_error:
            raise self.send_error
        self.sent.append((origin, chain))
        return self.send_result


class DummyEvent:
    unified_msg_origin = "onebot:group:123"
    message_str = "hello"
    is_at_or_wake_command = False

    def __init__(self, *, is_at_or_wake_command=False):
        self.is_at_or_wake_command = is_at_or_wake_command
        self.message_obj = types.SimpleNamespace(
            message=[],
            message_str="hello",
            self_id="bot",
            group_id="123",
            message_id="m1",
            timestamp=1000.0,
            sender=types.SimpleNamespace(user_id="u1", nickname="alice"),
        )

    def get_sender_id(self):
        return "u1"

    def get_sender_name(self):
        return "alice"

    def get_group_id(self):
        return "123"


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        LOGGER.records.clear()
        self.temp_dir = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def make_service(self, config=None, context=None):
        return MaiBotProactiveService(
            context=context or DummyContext(),
            config=PluginConfig(DummyConfig(config or {})),
            data_dir=Path(self.temp_dir.name),
        )

    async def test_send_reply_logs_success(self):
        context = DummyContext(send_result=True)
        service = self.make_service(context=context)
        await service._send_reply("onebot:group:123", "hello from proactive")
        self.assertEqual(len(context.sent), 1)
        self.assertTrue(any(record[0] == "info" and "send success" in record[1][0] for record in LOGGER.records))

    async def test_send_reply_raises_when_platform_send_returns_false(self):
        service = self.make_service(context=DummyContext(send_result=False))
        with self.assertRaises(RuntimeError):
            await service._send_reply("onebot:group:missing", "hello")

    async def test_send_reply_reraises_platform_exception(self):
        service = self.make_service(context=DummyContext(send_error=ValueError("boom")))
        with self.assertRaises(ValueError):
            await service._send_reply("onebot:group:123", "hello")

    async def test_core_wake_group_message_does_not_schedule_observation(self):
        service = self.make_service()
        service.running = True
        scheduled = []

        async def fake_schedule(origin, chat_type, trigger_reason):
            scheduled.append((origin, chat_type, trigger_reason))

        service._schedule_observation = fake_schedule
        await service.handle_event(DummyEvent(is_at_or_wake_command=True))
        self.assertEqual(scheduled, [])


if __name__ == "__main__":
    unittest.main()
