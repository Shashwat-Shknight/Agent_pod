"""Server-side browser sessions (HttpOnly cookie), replacing Streamlit's per-tab session_state.

- The token is random and opaque; nothing about the user lives in the cookie.
- Idle timeout (SESSION_IDLE_MINUTES) and an absolute lifetime both apply.
- A refresh (F5) keeps the user signed in, unlike the Streamlit app.
- Sessions are in memory: restarting the server signs everyone out (acceptable for one-process deployments).
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.settings import SESSION_IDLE_MINUTES

COOKIE_NAME = "nuucare_session"
ABSOLUTE_LIFETIME = timedelta(hours=12)
IDLE_TIMEOUT = timedelta(minutes=SESSION_IDLE_MINUTES)


@dataclass
class Session:
    token: str
    username: str
    created_at: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)

    def expired(self, now: datetime) -> str | None:
        if now - self.created_at > ABSOLUTE_LIFETIME:
            return "Your session reached its maximum length. Please sign in again."
        if now - self.last_seen > IDLE_TIMEOUT:
            return f"You were signed out after {SESSION_IDLE_MINUTES} minutes of inactivity."
        return None


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> Session:
        session = Session(token=secrets.token_urlsafe(32), username=username)
        with self._lock:
            self._sessions[session.token] = session
        return session

    def resolve(self, token: str | None, now: datetime | None = None) -> tuple[Session | None, str | None]:
        """Return (session, None) for a live session or (None, reason) when missing/expired."""
        if not token:
            return None, None
        now = now or datetime.now()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None, None
            reason = session.expired(now)
            if reason:
                self._sessions.pop(token, None)
                return None, reason
            session.last_seen = now
            return session, None

    def drop(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def drop_user(self, username: str, keep: str | None = None) -> int:
        """End every session of a user (deactivation, password reset), optionally keeping the caller's."""
        with self._lock:
            tokens = [t for t, s in self._sessions.items() if s.username == username and t != keep]
            for token in tokens:
                self._sessions.pop(token, None)
        return len(tokens)


SESSIONS = SessionStore()
