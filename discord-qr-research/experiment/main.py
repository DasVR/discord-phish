"""Discord remote auth research tool entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

import discord
import uvicorn
from discord import app_commands

from experiment.config import ConfigurationError, settings
from interface import bot as bot_module
from interface.commands import admin as admin_commands
from interface.commands import session as session_commands
from interface.state import store
from server.server import app

logger = logging.getLogger(__name__)


class HttpServerThread:
    """Runs uvicorn in a daemon thread on port 8080."""

    def __init__(self) -> None:
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        def run_server() -> None:
            config = uvicorn.Config(
                app,
                host=settings.qr_server_host,
                port=settings.qr_server_port,
                log_level=settings.log_level.lower(),
            )
            self._server = uvicorn.Server(config)
            self._server.run()

        self._thread = threading.Thread(target=run_server, daemon=True, name="fastapi-server")
        self._thread.start()
        logger.info(
            "FastAPI server started on http://%s:%s",
            settings.qr_server_host,
            settings.qr_server_port,
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)


class ResearchClient(discord.Client):
    """Discord client with slash command tree for research sessions."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await session_commands.setup(self)
        await admin_commands.setup(self)
        logger.info("Slash commands registered")

    async def on_ready(self) -> None:
        bot_module.BOT_STARTED_AT = datetime.now(timezone.utc)
        store._persisted = store._load_persisted()
        synced = await self.tree.sync()
        logger.info(
            "Logged in as %s (%s); synced %d command(s); sessions_enabled=%s",
            self.user.name if self.user else "unknown",
            self.user.id if self.user else "?",
            len(synced),
            store.enabled,
        )


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def close_active_sessions() -> None:
    """Close remote-auth WebSocket connections and cancel background tasks."""
    records = await store.list_records()
    for record in records:
        if record.client is not None:
            try:
                await record.client.close()
            except Exception:
                logger.exception("Failed closing client for session %s", record.session.session_id)

        if record.task is not None and not record.task.done():
            record.task.cancel()
            try:
                await record.task
            except asyncio.CancelledError:
                pass

    removed = await store.purge_expired()
    if removed:
        logger.info("Purged %d session(s) during shutdown", removed)


async def save_state() -> None:
    """Persist admin toggle and counters to state.json."""
    try:
        store._save_persisted()
        logger.info("Saved state to %s", settings.state_file)
    except Exception:
        logger.exception("Failed to save state")


async def graceful_shutdown(client: ResearchClient, http: HttpServerThread) -> None:
    logger.info("Shutting down…")
    await close_active_sessions()
    await save_state()
    http.stop()
    if not client.is_closed():
        await client.close()
    logger.info("Shutdown complete")


async def run() -> None:
    configure_logging()
    http = HttpServerThread()
    http.start()

    client = ResearchClient()
    shutdown_lock = asyncio.Lock()
    shutdown_started = False

    async def request_shutdown() -> None:
        nonlocal shutdown_started
        async with shutdown_lock:
            if shutdown_started:
                return
            shutdown_started = True
        await graceful_shutdown(client, http)

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(request_shutdown()))
        except NotImplementedError:
            pass

    try:
        await client.start(settings.discord_bot_token)
    except discord.LoginFailure as exc:
        logger.error("Discord login failed: %s", exc)
        raise
    except Exception:
        logger.exception("Unhandled error while running bot")
        raise
    finally:
        await request_shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
