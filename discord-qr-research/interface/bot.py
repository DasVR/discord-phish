"""discord.py bot — controls remote auth research test sessions."""

from __future__ import annotations

import asyncio
import logging
import sys

from datetime import datetime, timezone

import discord
from discord.ext import commands

from config import settings
from interface.commands import admin as admin_commands
from interface.commands import session as session_commands
from server.server import start_server

logger = logging.getLogger(__name__)

BOT_STARTED_AT: datetime | None = None


class ResearchBot(commands.Bot):
    """Discord bot with slash commands for session and admin control."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await session_commands.setup(self)
        await admin_commands.setup(self)
        await self.tree.sync()
        logger.info("Slash commands synced")

    async def on_ready(self) -> None:
        global BOT_STARTED_AT
        if BOT_STARTED_AT is None:
            BOT_STARTED_AT = datetime.now(timezone.utc)
        assert self.user is not None
        logger.info("Logged in as %s (%s)", self.user.name, self.user.id)


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def run_async() -> None:
    configure_logging()

    if not settings.discord_bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required")

    qr_runner = await start_server()

    bot = ResearchBot()
    try:
        await bot.start(settings.discord_bot_token)
    finally:
        await qr_runner.cleanup()


def run_bot() -> None:
    """Entry point for the Discord research bot."""
    asyncio.run(run_async())


if __name__ == "__main__":
    run_bot()
