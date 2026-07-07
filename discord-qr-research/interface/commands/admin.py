"""Admin slash commands with persistent toggle and session management."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

import discord
from discord import app_commands
from uuid import UUID

from config import settings
from interface import bot as bot_module
from interface.state import store

logger = logging.getLogger(__name__)


def _require_admin(interaction: discord.Interaction) -> bool:
    if not settings.admin_user_ids:
        return False
    return interaction.user.id in settings.admin_user_ids


async def _admin_only(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Admin commands can only be used inside a server.",
            ephemeral=True,
        )
        return False

    if not _require_admin(interaction):
        await interaction.response.send_message(
            "Admin access required. Configure `ADMIN_USER_IDS` with your Discord user ID.",
            ephemeral=True,
        )
        return False

    return True


def _format_uptime() -> str:
    started = bot_module.BOT_STARTED_AT
    if not started:
        return "Starting…"

    delta = datetime.now(timezone.utc) - started
    total = int(delta.total_seconds())
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


class AdminCommands(app_commands.Group):
    """Administrative controls for the research bot."""

    def __init__(self) -> None:
        super().__init__(
            name="admin",
            description="Administration commands",
            guild_only=True,
        )

    @app_commands.command(name="toggle", description="Enable or disable /session start globally")
    @app_commands.describe(mode="Turn session creation on or off")
    async def toggle(
        self,
        interaction: discord.Interaction,
        mode: Literal["on", "off"],
    ) -> None:
        if not await _admin_only(interaction):
            return

        enabled = mode == "on"
        await store.set_sessions_enabled(enabled)

        state_label = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            f"Session creation is now **{state_label}**.",
            ephemeral=True,
        )
        logger.info("Sessions toggled %s by user %s", state_label, interaction.user.id)

    @app_commands.command(name="status", description="Show bot and session feature status")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await _admin_only(interaction):
            return

        records = await store.list_records()
        active_count = len(records)
        toggle_state = "enabled" if store.enabled else "disabled"

        embed = discord.Embed(
            title="Research Bot Status",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Session Toggle", value=toggle_state.capitalize(), inline=True)
        embed.add_field(name="Active Sessions", value=str(active_count), inline=True)
        embed.add_field(name="Total Completed", value=str(store.total_completed), inline=True)
        embed.add_field(name="Bot Uptime", value=_format_uptime(), inline=False)
        embed.set_footer(text="Authorized security research")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="sessions", description="List active remote auth sessions")
    async def sessions(self, interaction: discord.Interaction) -> None:
        if not await _admin_only(interaction):
            return

        records = await store.list_records()
        if not records:
            await interaction.response.send_message("No active sessions.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Active Sessions ({len(records)})",
            color=discord.Color.dark_grey(),
            timestamp=datetime.now(timezone.utc),
        )

        for record in records[:10]:
            session = record.session
            owner = record.owner_name or (str(record.owner_id) if record.owner_id else "Unknown")
            embed.add_field(
                name=str(session.session_id),
                value=(
                    f"**User:** {owner}\n"
                    f"**Status:** {session.state.value}\n"
                    f"**Age:** {record.age_seconds()}s"
                ),
                inline=False,
            )

        if len(records) > 10:
            embed.set_footer(text=f"Showing 10 of {len(records)} sessions")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="kill", description="Force-end an active session")
    @app_commands.describe(session_id="UUID of the session to terminate")
    async def kill(self, interaction: discord.Interaction, session_id: str) -> None:
        if not await _admin_only(interaction):
            return

        try:
            sid = UUID(session_id.strip())
        except ValueError:
            await interaction.response.send_message("Invalid session UUID.", ephemeral=True)
            return

        from interface.commands.session import stop_controller

        await stop_controller(sid)
        killed = await store.kill_session(sid, reason="Force-killed by admin")

        if not killed:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Session `{sid}` has been terminated.",
            ephemeral=True,
        )
        logger.info("Session %s killed by admin %s", sid, interaction.user.id)


async def setup(bot: discord.Client) -> AdminCommands:
    group = AdminCommands()
    bot.tree.add_command(group)
    return group
