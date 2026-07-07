"""WebSocket implementation of Discord's documented remote auth desktop protocol."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID

import aiohttp
import websockets
from websockets.asyncio.client import ClientConnection

from config import settings
from research.crypto import KeyPair, build_nonce_proof, generate_keypair

logger = logging.getLogger(__name__)


class ProtocolState(str, Enum):
    """Lifecycle states for a remote auth session."""

    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    READY = "ready"
    AWAITING_SCAN = "awaiting_scan"
    PENDING_TICKET = "pending_ticket"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass
class RemoteUserPreview:
    """User metadata received in pending_ticket (pre-login confirmation)."""

    user_id: str
    discriminator: str
    avatar_hash: str
    username: str

    @classmethod
    def from_payload(cls, payload: str) -> RemoteUserPreview:
        parts = payload.split(":")
        if len(parts) < 4:
            raise ValueError(f"Unexpected user payload format: {payload!r}")
        return cls(
            user_id=parts[0],
            discriminator=parts[1],
            avatar_hash=parts[2],
            username=":".join(parts[3:]),
        )


@dataclass
class RemoteAuthSession:
    """In-memory representation of one remote auth research session."""

    session_id: UUID
    state: ProtocolState = ProtocolState.CONNECTING
    fingerprint: Optional[str] = None
    qr_url: Optional[str] = None
    user_preview: Optional[RemoteUserPreview] = None
    token: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None
    keypair: Optional[KeyPair] = None

    def seconds_remaining(self) -> int:
        if not self.expires_at:
            return settings.session_ttl_seconds
        delta = self.expires_at - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds()))


class RemoteAuthClient:
    """
    Async client for wss://remote-auth-gateway.discord.gg/?v=2

    Implements the publicly documented handshake:
    hello -> init -> nonce_proof -> pending_remote_init -> ... -> pending_login
    """

    WS_ORIGIN = "https://discord.com"

    def __init__(
        self,
        session: RemoteAuthSession,
        gateway_url: str | None = None,
        api_base: str | None = None,
    ) -> None:
        self.session = session
        self.gateway_url = gateway_url or settings.remote_auth_gateway
        self.api_base = api_base or settings.discord_api_base
        self._ws: ClientConnection | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_interval_ms = 0
        self._closed = asyncio.Event()

    async def run(self) -> None:
        """Connect, complete handshake, and wait until login completes or fails."""
        self.session.keypair = generate_keypair()
        self.session.state = ProtocolState.CONNECTING

        try:
            async with websockets.connect(
                self.gateway_url,
                origin=self.WS_ORIGIN,
                open_timeout=30,
                close_timeout=10,
            ) as ws:
                self._ws = ws
                self.session.state = ProtocolState.HANDSHAKING
                await self._listen(ws)
        except asyncio.CancelledError:
            self.session.state = ProtocolState.EXPIRED
            self.session.error = "Session cancelled"
            raise
        except Exception as exc:
            logger.exception("Remote auth session %s failed", self.session.session_id)
            self.session.state = ProtocolState.ERROR
            self.session.error = str(exc)
        finally:
            self._closed.set()
            await self._stop_heartbeat()

    async def _listen(self, ws: ClientConnection) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid JSON from gateway: %s", raw)
                raise ValueError("Gateway sent invalid JSON") from exc

            op = message.get("op")
            logger.debug("Session %s recv op=%s", self.session.session_id, op)
            await self._dispatch(op, message)

            if self.session.state in {
                ProtocolState.COMPLETED,
                ProtocolState.CANCELLED,
                ProtocolState.ERROR,
                ProtocolState.EXPIRED,
            }:
                break

    async def _dispatch(self, op: str | None, message: dict[str, Any]) -> None:
        if op == "hello":
            await self._handle_hello(message)
        elif op == "heartbeat_ack":
            logger.debug("Heartbeat acknowledged")
        elif op == "nonce_proof":
            await self._handle_nonce_proof(message)
        elif op == "pending_remote_init":
            await self._handle_pending_remote_init(message)
        elif op == "pending_ticket":
            await self._handle_pending_ticket(message)
        elif op == "pending_login":
            await self._handle_pending_login(message)
        elif op == "cancel":
            await self._handle_cancel()
        else:
            logger.warning("Unhandled gateway opcode: %s", op)

    async def _handle_hello(self, message: dict[str, Any]) -> None:
        interval = message.get("heartbeat_interval")
        if not isinstance(interval, int) or interval <= 0:
            raise ValueError("hello missing valid heartbeat_interval")

        self._heartbeat_interval_ms = interval
        await self._start_heartbeat(interval / 1000)
        await self._send(
            "init",
            {"encoded_public_key": self.session.keypair.encoded_public_key()},  # type: ignore[union-attr]
        )

    async def _handle_nonce_proof(self, message: dict[str, Any]) -> None:
        encrypted_nonce = message.get("encrypted_nonce")
        if not encrypted_nonce:
            raise ValueError("nonce_proof missing encrypted_nonce")

        decrypted = self.session.keypair.decrypt_oaep(encrypted_nonce)  # type: ignore[union-attr]
        proof = build_nonce_proof(decrypted)
        await self._send("nonce_proof", {"proof": proof})

    async def _handle_pending_remote_init(self, message: dict[str, Any]) -> None:
        from datetime import timedelta

        fingerprint = message.get("fingerprint")
        if not fingerprint:
            raise ValueError("pending_remote_init missing fingerprint")

        self.session.fingerprint = fingerprint
        self.session.qr_url = f"https://discord.com/ra/{fingerprint}"
        self.session.state = ProtocolState.AWAITING_SCAN
        self.session.expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.session_ttl_seconds
        )
        logger.info(
            "Session %s ready — fingerprint %s",
            self.session.session_id,
            fingerprint[:12],
        )

    async def _handle_pending_ticket(self, message: dict[str, Any]) -> None:
        encrypted_payload = message.get("encrypted_user_payload")
        if not encrypted_payload:
            raise ValueError("pending_ticket missing encrypted_user_payload")

        plaintext = self.session.keypair.decrypt_oaep(encrypted_payload).decode("utf-8")  # type: ignore[union-attr]
        self.session.user_preview = RemoteUserPreview.from_payload(plaintext)
        self.session.state = ProtocolState.PENDING_TICKET
        logger.info(
            "Session %s pending ticket for user %s",
            self.session.session_id,
            self.session.user_preview.username,
        )

    async def _handle_pending_login(self, message: dict[str, Any]) -> None:
        ticket = message.get("ticket")
        if not ticket:
            raise ValueError("pending_login missing ticket")

        encrypted_token = await self._exchange_ticket(ticket)
        if not encrypted_token:
            raise ValueError("remote-auth/login did not return encrypted_token")

        token_bytes = self.session.keypair.decrypt_oaep(encrypted_token)  # type: ignore[union-attr]
        self.session.token = token_bytes.decode("utf-8")
        self.session.state = ProtocolState.COMPLETED
        logger.info("Session %s completed authentication", self.session.session_id)

    async def _handle_cancel(self) -> None:
        self.session.state = ProtocolState.CANCELLED
        self.session.error = "Login cancelled on mobile device"
        logger.info("Session %s cancelled by mobile client", self.session.session_id)

    async def _exchange_ticket(self, ticket: str) -> str | None:
        url = f"{self.api_base}/users/@me/remote-auth/login"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "DiscordRemoteAuthResearch/1.0",
        }
        async with aiohttp.ClientSession() as http:
            async with http.post(url, json={"ticket": ticket}, headers=headers) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.error("Ticket exchange failed HTTP %s: %s", resp.status, body)
                    return None
                data = json.loads(body)
                token = data.get("encrypted_token")
                return str(token) if token else None

    async def _send(self, op: str, payload: dict[str, Any] | None = None) -> None:
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        message: dict[str, Any] = {"op": op}
        if payload:
            message.update(payload)

        logger.debug("Session %s send op=%s", self.session.session_id, op)
        await self._ws.send(json.dumps(message))

    async def _start_heartbeat(self, interval_seconds: float) -> None:
        await self._stop_heartbeat()

        async def heartbeat_loop() -> None:
            while not self._closed.is_set():
                await asyncio.sleep(interval_seconds)
                try:
                    await self._send("heartbeat")
                except Exception:
                    logger.debug("Heartbeat stopped — connection closed")
                    return

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._heartbeat_task = None

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._closed.set()
        await self._stop_heartbeat()
        if self._ws:
            await self._ws.close()
