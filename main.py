from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig

from .maibot_proactive.config import PluginConfig
from .maibot_proactive.runtime import MaiBotProactiveService


def load_metadata() -> Dict[str, Any]:
    metadata_path = Path(__file__).parent / "metadata.yaml"
    if not metadata_path.exists():
        return {}
    with metadata_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


_metadata = load_metadata()


@register(
    _metadata.get("name", "astrbot_plugin_maibot_proactive"),
    _metadata.get("author", "OpenAI Codex"),
    _metadata.get("desc", "MaiBot proactive chat plugin"),
    _metadata.get("version", "v0.1.0"),
    _metadata.get("repo", ""),
)
class MaiBotProactivePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.plugin_config = PluginConfig(config)
        try:
            data_dir = Path(StarTools.get_data_dir())
        except Exception:
            data_dir = Path(__file__).parent / "data"
        self.service = MaiBotProactiveService(
            context=context,
            config=self.plugin_config,
            data_dir=data_dir,
        )

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self.service.start()
        logger.info("MaiBot proactive plugin started")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent) -> None:
        await self.service.handle_event(event)

    async def terminate(self) -> None:
        await self.service.shutdown()
        logger.info("MaiBot proactive plugin stopped")
