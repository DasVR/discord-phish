"""In-memory session state with JSON persistence for admin controls."""

from __future__ import annotations

import asyncio
import threading
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Union
from uuid import UUID, uuid4

from config import settings
from research.protocol import ProtocolState, RemoteAuthClient, RemoteAuthSession
from research.qr_generator import generate_qr_bytes

logger = logging.getLogger(__name__)


@dataclass
class PersistedState:
    """Fields stored in state.json between restarts."""

    sessions_enabled: bool = True
    total_completed: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersistedState:
        return cls(
            sessions_enabled=bool(data.get("sessions_enabled", True)),
            total_completed=int(data.get("total_completed", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions_enabled": self.sessions_enabled,
            "total_completed": self.total_completed,
        }


@dataclass
class SessionRecord:
    """Stored session with optional background protocol task."""

    session: RemoteAuthSession
    owner_id: Optional[int] = None
    owner_name: Optional[str] = None
    qr_png: Optional[bytes] = None
    task: Optional[asyncio.Task[None]] = None
    client: Optional[RemoteAuthClient] = None
    on_complete: Optional[Callable[[RemoteAuthSession], Union[Awaitable[None], None]]] = None
    generation: int = 0
    embed_active: bool = False
    last_handshake_at: Optional[datetime] = None

    def age_seconds(self) -> int:
        delta = datetime.now(timezone.utc) - self.session.created_at
        return max(0, int(delta.total_seconds()))


class SessionStore:
    """Thread-safe in-memory store keyed by session UUID."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, SessionRecord] = {}
        self._lock = threading.RLock()
        self._persisted = self._load_persisted()

    @property
    def enabled(self) -> bool:
        return self._persisted.sessions_enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._persisted.sessions_enabled = value
        self._save_persisted()

    @property
    def total_completed(self) -> int:
        return self._persisted.total_completed

    def _load_persisted(self) -> PersistedState:
        path = settings.state_file
        if not path.exists():
            return PersistedState()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return PersistedState.from_dict(data)
        except Exception:
            logger.exception("Failed to load %s — using defaults", path)
            return PersistedState()

    def _save_persisted(self) -> None:
        path = settings.state_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._persisted.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to save persisted state to %s", path)

    async def set_sessions_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._persisted.sessions_enabled = enabled
            self._save_persisted()

    async def increment_completed(self) -> None:
        with self._lock:
            self._persisted.total_completed += 1
            self._save_persisted()

    async def create_session(
        self,
        owner_id: Optional[int] = None,
        owner_name: Optional[str] = None,
    ) -> RemoteAuthSession:
        with self._lock:
            session = RemoteAuthSession(session_id=uuid4())
            self._sessions[session.session_id] = SessionRecord(
                session=session,
                owner_id=owner_id,
                owner_name=owner_name,
            )
            logger.info("Created session %s for owner %s", session.session_id, owner_id)
            return session

    async def get(self, session_id: UUID) -> Optional[SessionRecord]:
        with self._lock:
            return self._sessions.get(session_id)

    async def remove(self, session_id: UUID) -> None:
        await self.kill_session(session_id, reason="Session removed")

    async def list_records(self) -> list[SessionRecord]:
        with self._lock:
            return list(self._sessions.values())

    async def list_active(self) -> list[RemoteAuthSession]:
        with self._lock:
            return [record.session for record in self._sessions.values()]

    async def start_protocol(
        self,
        session_id: UUID,
        on_complete: Callable[[RemoteAuthSession], Union[Awaitable[None], None]] | None = None,
    ) -> RemoteAuthSession:
        record = await self.get(session_id)
        if not record:
            raise KeyError(f"Session {session_id} not found")

        if record.task and not record.task.done():
            return record.session

        record.on_complete = on_complete
        client = RemoteAuthClient(record.session)
        record.client = client

        async def runner() -> None:
            try:
                await client.run()
            finally:
                if record.session.fingerprint and not record.qr_png:
                    record.qr_png = generate_qr_bytes(record.session.fingerprint)
                if record.session.state == ProtocolState.COMPLETED:
                    await self.increment_completed()
                if record.on_complete:
                    try:
                        result = record.on_complete(record.session)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception("Session completion callback failed")
                await client.close()
                record.client = None

        record.task = asyncio.create_task(runner())
        record.last_handshake_at = datetime.now(timezone.utc)
        return record.session

    async def restart_protocol(
        self,
        session_id: UUID,
        on_complete: Callable[[RemoteAuthSession], Union[Awaitable[None], None]] | None = None,
    ) -> RemoteAuthSession:
        """Cancel the current gateway connection and run a fresh handshake."""
        record = await self.get(session_id)
        if not record:
            raise KeyError(f"Session {session_id} not found")

        if record.client:
            await record.client.close()

        if record.task and not record.task.done():
            record.task.cancel()
            try:
                await record.task
            except asyncio.CancelledError:
                pass

        session = record.session
        session.state = ProtocolState.CONNECTING
        session.fingerprint = None
        session.qr_url = None
        session.user_preview = None
        session.token = None
        session.error = None
        session.expires_at = None
        session.keypair = None
        record.qr_png = None
        record.client = None
        record.generation += 1

        if on_complete is not None:
            record.on_complete = on_complete

        return await self.start_protocol(session_id, record.on_complete)

    async def kill_session(self, session_id: UUID, *, reason: str = "Force-killed by admin") -> bool:
        """Force-end a session and close its WebSocket connection."""
        with self._lock:
            record = self._sessions.get(session_id)
            if not record:
                return False

        if record.client:
            await record.client.close()

        if record.task and not record.task.done():
            record.task.cancel()
            try:
                await record.task
            except asyncio.CancelledError:
                pass

        record.session.state = ProtocolState.EXPIRED
        record.session.error = reason
        record.embed_active = False
        record.client = None

        with self._lock:
            self._sessions.pop(session_id, None)

        logger.info("Killed session %s (%s)", session_id, reason)
        return True

    async def wait_for_qr(self, session_id: UUID, timeout: float = 30.0) -> bytes:
        """Block until the session fingerprint is ready and return QR PNG bytes."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = await self.get(session_id)
            if not record:
                raise KeyError(f"Session {session_id} not found")

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
                raise RuntimeError(record.session.error or "Session failed before QR was ready")

            await asyncio.sleep(0.5)

        raise TimeoutError("Timed out waiting for QR fingerprint")

    async def purge_expired(self) -> int:
        """Remove sessions past expiry or in terminal states."""
        now = datetime.now(timezone.utc)
        to_cleanup: list[SessionRecord] = []

        with self._lock:
            to_delete: list[UUID] = []
            for sid, record in self._sessions.items():
                expired = (
                    record.session.expires_at is not None and record.session.expires_at < now
                )
                terminal = record.session.state in {
                    ProtocolState.COMPLETED,
                    ProtocolState.ERROR,
                    ProtocolState.CANCELLED,
                    ProtocolState.EXPIRED,
                }
                if record.embed_active:
                    continue
                if expired or terminal:
                    to_delete.append(sid)

            for sid in to_delete:
                record = self._sessions.pop(sid, None)
                if record:
                    to_cleanup.append(record)

        for record in to_cleanup:
            if record.task and not record.task.done():
                record.task.cancel()
            if record.client:
                await record.client.close()

        removed = len(to_cleanup)
        if removed:
            logger.info("Purged %d session(s)", removed)
        return removed


store = SessionStore()
