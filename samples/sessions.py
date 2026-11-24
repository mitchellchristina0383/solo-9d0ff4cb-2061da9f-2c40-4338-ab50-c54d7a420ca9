"""In-memory login sessions for the sample legacy application.

The sample app mimics two old applications that both remember a login with a
cookie named ``sid``.  Sessions live in process memory behind a lock; the
Compose deployment runs each sample with a single gunicorn worker so every
request reaches the same store.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Session:
    sid: str
    username: str
    created_at: float
    expires_at: float | None  # None == browser session cookie

    def is_valid(self, now: float | None = None) -> bool:
        return self.expires_at is None or (now or time.time()) < self.expires_at


class SessionStore:
    """Thread-safe sid -> Session map with bounded growth."""

    def __init__(self, max_sessions: int = 5000) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._max = max_sessions

    def create(self, username: str, ttl_seconds: int | None) -> Session:
        now = time.time()
        session = Session(
            sid=secrets.token_urlsafe(24),
            username=username,
            created_at=now,
            expires_at=None if ttl_seconds is None else now + ttl_seconds,
        )
        with self._lock:
            self._prune_locked(now)
            self._sessions[session.sid] = session
            # Drop the oldest session if the store would grow unbounded.
            if len(self._sessions) > self._max:
                oldest = min(self._sessions, key=lambda key: self._sessions[key].created_at)
                self._sessions.pop(oldest, None)
        return session

    def get(self, sid: str) -> Session | None:
        if not sid:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if not session.is_valid(now):
                # Expired server-side state is removed on first use.
                self._sessions.pop(sid, None)
                return None
            return session

    def revoke(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _prune_locked(self, now: float) -> None:
        expired = [
            sid
            for sid, session in self._sessions.items()
            if session.expires_at is not None and session.expires_at <= now
        ]
        for sid in expired:
            self._sessions.pop(sid, None)


def parse_cookie_sids(cookie_header: str | None) -> list[tuple[str, str]]:
    """Split a Cookie header into ordered ``(name, value)`` pairs.

    The browser may send more than one ``sid`` cookie for one request (for
    example when two paths overlap).  The order is the order the browser sent
    them; callers decide which value wins.
    """
    pairs: list[tuple[str, str]] = []
    if not cookie_header:
        return pairs
    for chunk in cookie_header.split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            pairs.append((name, value))
    return pairs


def sid_values(cookie_header: str | None) -> list[str]:
    """Return every ``sid`` value from a Cookie header, in request order."""
    return [value for name, value in parse_cookie_sids(cookie_header) if name == "sid"]
