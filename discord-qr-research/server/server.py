"""Minimal FastAPI server for QR images and session status during research."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from config import settings
from interface.state import store
from research.protocol import ProtocolState, RemoteAuthSession
from research.qr_generator import generate_qr_bytes

logger = logging.getLogger(__name__)

PURGE_INTERVAL_SECONDS = 60


def _status_label(session: RemoteAuthSession) -> str:
    if session.state == ProtocolState.COMPLETED:
        return "Complete"
    if session.state == ProtocolState.PENDING_TICKET:
        return "Scanned — confirm on phone"
    if session.state in {ProtocolState.CANCELLED, ProtocolState.ERROR}:
        return session.error or session.state.value.replace("_", " ").title()
    if session.state == ProtocolState.EXPIRED or session.seconds_remaining() <= 0:
        return "Expired"
    if session.state in {
        ProtocolState.CONNECTING,
        ProtocolState.HANDSHAKING,
        ProtocolState.READY,
        ProtocolState.AWAITING_SCAN,
    }:
        return "Awaiting scan"
    return session.state.value.replace("_", " ").title()


async def _resolve_qr_png(session_id: UUID) -> bytes:
    record = await store.get(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Session not found")

    if record.qr_png:
        return record.qr_png

    if record.session.fingerprint:
        record.qr_png = generate_qr_bytes(record.session.fingerprint)
        return record.qr_png

    if record.session.state in {
        ProtocolState.ERROR,
        ProtocolState.CANCELLED,
        ProtocolState.EXPIRED,
    }:
        raise HTTPException(
            status_code=410,
            detail=record.session.error or "Session ended before QR was available",
        )

    raise HTTPException(status_code=503, detail="QR not ready yet")


def _qr_page_html(
    session_id: UUID,
    status: str,
    status_class: str,
    qr_ready: bool,
    expires_in: int,
) -> str:
    cache_bust = int(time.time())
    qr_block = (
        f'<img src="/qr/{session_id}.png?t={cache_bust}" '
        f'alt="Remote auth QR code" width="220" height="220">'
        if qr_ready
        else '<span class="placeholder">Preparing QR code…</span>'
    )
    timer = f"Expires in {expires_in}s" if expires_in > 0 else "Session ended"
    status_classes = f"status {status_class}".strip()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="10">
  <title>Remote Auth — {session_id}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #1e1f22;
      color: #f2f3f5;
      font-family: "gg sans", "Noto Sans", system-ui, sans-serif;
    }}
    .card {{
      width: min(420px, 92vw);
      padding: 28px;
      text-align: center;
      background: #2b2d31;
      border: 1px solid #3f4147;
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 20px;
      font-weight: 600;
    }}
    .subtitle {{
      margin: 0 0 20px;
      color: #b5bac1;
      font-size: 14px;
      line-height: 1.5;
    }}
    .qr {{
      width: 240px;
      height: 240px;
      margin: 0 auto 16px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #ffffff;
      border-radius: 10px;
      overflow: hidden;
    }}
    .qr img {{
      width: 100%;
      height: 100%;
      object-fit: contain;
    }}
    .placeholder {{
      color: #4e5058;
      font-size: 14px;
      padding: 0 16px;
    }}
    .status {{
      display: inline-block;
      padding: 6px 12px;
      border-radius: 999px;
      background: #5865f2;
      color: #ffffff;
      font-size: 13px;
      font-weight: 600;
      margin-bottom: 8px;
    }}
    .status.done {{ background: #248046; }}
    .status.warn {{ background: #f0b232; color: #1e1f22; }}
    .status.error {{ background: #da373c; }}
    .meta {{
      color: #949ba4;
      font-size: 13px;
      margin: 4px 0;
    }}
    .session-id {{
      margin-top: 12px;
      color: #72767d;
      font-size: 11px;
      word-break: break-all;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Remote Auth Session</h1>
    <p class="subtitle">Scan with Discord mobile: Settings → Scan QR Code</p>
    <div class="qr">{qr_block}</div>
    <div class="{status_classes}">{status}</div>
    <p class="meta">{timer}</p>
    <p class="meta">Page refreshes every 10 seconds</p>
    <p class="session-id">{session_id}</p>
  </div>
</body>
</html>"""


def _status_css_class(session: RemoteAuthSession) -> str:
    if session.state == ProtocolState.COMPLETED:
        return "done"
    if session.state == ProtocolState.PENDING_TICKET:
        return "warn"
    if session.state in {ProtocolState.ERROR, ProtocolState.CANCELLED, ProtocolState.EXPIRED}:
        return "error"
    return ""


async def _maintenance_loop() -> None:
    while True:
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
        try:
            await store.purge_expired()
        except Exception:
            logger.exception("Session purge failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_maintenance_loop())
    logger.info(
        "QR server listening on http://%s:%s",
        settings.qr_server_host,
        settings.qr_server_port,
    )
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Remote Auth Research QR Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.get("/health")
async def health() -> dict[str, object]:
    sessions = await store.list_active()
    return {"status": "ok", "active_sessions": len(sessions)}


@app.get("/qr/{session_id}.png")
async def qr_png(session_id: UUID) -> Response:
    png = await _resolve_qr_png(session_id)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/qr/{session_id}", response_class=HTMLResponse)
async def qr_page(session_id: UUID) -> HTMLResponse:
    record = await store.get(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Session not found")

    session = record.session
    status = _status_label(session)
    qr_ready = record.qr_png is not None or session.fingerprint is not None
    expires_in = session.seconds_remaining()

    html = _qr_page_html(
        session_id,
        status,
        _status_css_class(session),
        qr_ready,
        expires_in,
    )
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


@dataclass
class ServerRunner:
    """Handle returned to the bot for graceful shutdown."""

    _server: uvicorn.Server
    _task: asyncio.Task[None]

    async def cleanup(self) -> None:
        self._server.should_exit = True
        await self._task


async def start_server() -> ServerRunner:
    """Start uvicorn in the background on the configured host/port."""
    config = uvicorn.Config(
        app,
        host=settings.qr_server_host,
        port=settings.qr_server_port,
        log_level=settings.log_level.lower(),
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    return ServerRunner(_server=server, _task=task)
