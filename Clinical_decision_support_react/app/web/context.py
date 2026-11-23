"""Who is calling and what they may do. Every /api route depends on one of these.

Permission checks live on the server: hiding a button in React is a convenience, never the control.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from fastapi import Depends, Request

from app.domain import access_control as ac
from app.domain.settings import ACCESS_STORE_PATH
from app.web.errors import ApiError
from app.web.sessions import COOKIE_NAME, SESSIONS

# One writer at a time for the access store (users, permissions, audit). Every mutation re-reads first.
_STORE_LOCK = threading.RLock()

# Test-only switch: the legacy route tests call endpoints without signing in. Never set this in a deployment.
AUTH_DISABLED_ENV = "NUUCARE_AUTH_DISABLED"
OPEN_PATHS = {"/health"}  # liveness probe stays reachable without a session


def load_store() -> dict[str, Any]:
    return ac.load_store(ACCESS_STORE_PATH)


@contextmanager
def store_transaction() -> Iterator[dict[str, Any]]:
    """Re-read the store, let the caller change it, then save it - all under one lock (no lost updates)."""
    with _STORE_LOCK:
        store = load_store()
        yield store
        ac.save_store(ACCESS_STORE_PATH, store)


def audit(actor: str, action: str, detail: str = "") -> None:
    with store_transaction() as store:
        ac.audit(store, actor, action, detail)


@dataclass
class Caller:
    user: dict[str, Any]
    store: dict[str, Any]
    token: str

    @property
    def username(self) -> str:
        return self.user["username"]

    @property
    def name(self) -> str:
        return self.user.get("name") or self.user["username"]

    def can(self, module: str, action: str = "View") -> bool:
        return ac.can(self.store, self.user, module, action)

    @property
    def masking(self):
        from app.domain.masking import Policy

        return Policy.for_caller(self)

    def require_phi(self, phi: str, what: str) -> None:
        """Writes must not be based on masked values: you cannot change what you are not allowed to see."""
        from app.domain.masking import CLASS_LABELS

        if self.masking.hides(phi):
            raise ApiError(403, "phi_masked", f"Your role sees {CLASS_LABELS[phi]} masked, so it cannot change {what}.")

    def require(self, module: str, action: str = "View") -> None:
        if not self.can(module, action):
            raise ApiError(403, "forbidden", f"Your role ({self.user.get('role')}) cannot do this ({module} · {action}).")


def _resolve(request: Request, allow_password_change: bool) -> Caller:
    token = request.cookies.get(COOKIE_NAME)
    session, reason = SESSIONS.resolve(token)
    if session is None:
        raise ApiError(401, "session_expired" if reason else "unauthenticated", reason or "Please sign in.")
    store = load_store()
    user = ac.get_user(store, session.username)
    if user is None or not user.get("active", True):
        SESSIONS.drop(token)
        raise ApiError(401, "account_inactive", "Your account is no longer active. Contact an administrator.")
    if user.get("must_change_password") and not allow_password_change:
        raise ApiError(403, "password_change_required", "Change your temporary password to continue.")
    caller = Caller(user=ac.public_user(user), store=store, token=token or "")
    request.state.caller = caller  # read by the PHI masking middleware (app/main.py) after the route answers
    return caller


def current_caller(request: Request) -> Caller:
    return _resolve(request, allow_password_change=False)


def caller_allowing_password_change(request: Request) -> Caller:
    return _resolve(request, allow_password_change=True)


def requires(module: str, action: str = "View"):
    """Dependency factory: Depends(requires("Codes", "Edit"))."""

    def dependency(caller: Caller = Depends(current_caller)) -> Caller:
        caller.require(module, action)
        return caller

    return dependency


def legacy_route_guard(request: Request) -> None:
    """Guard for the pre-React endpoints (/encounters, /codes, /ehr, /patients, /reference, /vbc).

    They used to be wide open on the network. Now they need a signed-in, active user.
    """
    if request.url.path in OPEN_PATHS or os.environ.get(AUTH_DISABLED_ENV) == "1":
        return
    _resolve(request, allow_password_change=False)
