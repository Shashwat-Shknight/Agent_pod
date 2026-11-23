"""SECURITY schema repository: users, role permissions, audit trail.

The Streamlit router reloads the access store on every rerun, so reads are served from a
process-wide snapshot refreshed every ``CACHE_SECONDS`` (and immediately after this process
writes). Writes send only what changed relative to that snapshot, so one session saving its
copy cannot revert another session's edit to a different user.
"""

from __future__ import annotations

import copy
import hashlib
import threading
import time
from typing import Any

from app.db import client

CACHE_SECONDS = 15
AUDIT_WINDOW = 2000  # events kept in the in-app view; the table itself keeps everything

_LOCK = threading.RLock()
_SNAPSHOT: dict[str, Any] | None = None
_LOADED_AT = 0.0


def _event_ids(events: list[dict[str, Any]]) -> list[str]:
    """Deterministic, idempotent IDs: identical events get an occurrence suffix."""
    seen: dict[str, int] = {}
    ids = []
    for event in events:
        base = client.to_json({k: event.get(k) for k in ("at", "actor", "action", "detail")})
        seen[base] = seen.get(base, 0) + 1
        ids.append(hashlib.sha256(f"{base}#{seen[base]}".encode("utf-8")).hexdigest())
    return ids


def _fetch() -> dict[str, Any]:
    users = [client.variant(row["USER_JSON"]) for row in client.query(
        "SELECT USER_JSON FROM SECURITY.APP_USERS ORDER BY USERNAME")]
    permissions = {
        row["ROLE"]: client.variant(row["PERMISSIONS"])
        for row in client.query("SELECT ROLE, PERMISSIONS FROM SECURITY.ROLE_PERMISSIONS")
    }
    meta_rows = client.query("SELECT SETTING_VALUE FROM SECURITY.STORE_SETTINGS WHERE SETTING_KEY = 'store_meta'")
    audit_rows = client.query(
        "SELECT EVENT_JSON FROM (SELECT EVENT_JSON, INSERTED_AT, EVENT_AT FROM SECURITY.AUDIT_LOG "
        "ORDER BY INSERTED_AT DESC, EVENT_AT DESC LIMIT %s) ORDER BY INSERTED_AT, EVENT_AT",
        (AUDIT_WINDOW,),
    )
    store = dict(client.variant(meta_rows[0]["SETTING_VALUE"]) or {}) if meta_rows else {}
    store.setdefault("version", 1)
    store["users"] = [user for user in users if isinstance(user, dict)]
    store["role_permissions"] = permissions
    store["audit"] = [client.variant(row["EVENT_JSON"]) for row in audit_rows]
    return store


def load_store(force: bool = False) -> dict[str, Any]:
    """Raw store dict (callers apply defaults). Returns a private deep copy."""
    global _SNAPSHOT, _LOADED_AT
    with _LOCK:
        if force or _SNAPSHOT is None or time.monotonic() - _LOADED_AT > CACHE_SECONDS:
            _SNAPSHOT = _fetch()
            _LOADED_AT = time.monotonic()
        return copy.deepcopy(_SNAPSHOT)


def save_store(store: dict[str, Any]) -> None:
    global _SNAPSHOT, _LOADED_AT
    with _LOCK:
        baseline = _SNAPSHOT if _SNAPSHOT is not None else _fetch()
        before_users = {user.get("username"): client.to_json(user) for user in baseline.get("users", [])}
        changed_users = [
            user for user in store.get("users", [])
            if user.get("username") and before_users.get(user["username"]) != client.to_json(user)
        ]
        before_perms = baseline.get("role_permissions", {})
        changed_perms = {
            role: perms for role, perms in (store.get("role_permissions") or {}).items()
            if client.to_json(before_perms.get(role)) != client.to_json(perms)
        }
        known_events = set(_event_ids(baseline.get("audit", [])))
        events = store.get("audit", [])
        new_events = [
            {"id": event_id, **event}
            for event_id, event in zip(_event_ids(events), events)
            if event_id not in known_events
        ]
        meta = {key: value for key, value in store.items() if key not in {"users", "role_permissions", "audit"}}
        meta_changed = client.to_json(meta) != client.to_json(
            {key: value for key, value in baseline.items() if key not in {"users", "role_permissions", "audit"}}
        )
        if not (changed_users or changed_perms or new_events or meta_changed):
            return

        with client.transaction() as cursor:
            if changed_users:
                cursor.execute(
                    "MERGE INTO SECURITY.APP_USERS t USING ("
                    "  SELECT f.value AS V FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s))) f"
                    ") s ON t.USERNAME = s.V:username::STRING "
                    "WHEN MATCHED THEN UPDATE SET NAME = s.V:name::STRING, EMAIL = s.V:email::STRING, "
                    "  ROLE = s.V:role::STRING, ACTIVE = COALESCE(s.V:active::BOOLEAN, TRUE), USER_JSON = s.V, "
                    "  UPDATED_AT = CURRENT_TIMESTAMP() "
                    "WHEN NOT MATCHED THEN INSERT (USERNAME, NAME, EMAIL, ROLE, ACTIVE, USER_JSON) VALUES ("
                    "  s.V:username::STRING, s.V:name::STRING, s.V:email::STRING, s.V:role::STRING, "
                    "  COALESCE(s.V:active::BOOLEAN, TRUE), s.V)",
                    (client.to_json(changed_users),),
                )
            if changed_perms:
                cursor.execute(
                    "MERGE INTO SECURITY.ROLE_PERMISSIONS t USING ("
                    "  SELECT f.key AS ROLE, f.value AS PERMISSIONS FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s))) f"
                    ") s ON t.ROLE = s.ROLE "
                    "WHEN MATCHED THEN UPDATE SET PERMISSIONS = s.PERMISSIONS, UPDATED_AT = CURRENT_TIMESTAMP() "
                    "WHEN NOT MATCHED THEN INSERT (ROLE, PERMISSIONS) VALUES (s.ROLE, s.PERMISSIONS)",
                    (client.to_json(changed_perms),),
                )
            if new_events:
                cursor.execute(
                    "MERGE INTO SECURITY.AUDIT_LOG t USING ("
                    "  SELECT f.value AS V FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s))) f"
                    ") s ON t.EVENT_ID = s.V:id::STRING "
                    "WHEN NOT MATCHED THEN INSERT (EVENT_ID, EVENT_AT, ACTOR, ACTION, DETAIL, EVENT_JSON) VALUES ("
                    "  s.V:id::STRING, s.V:at::STRING, s.V:actor::STRING, s.V:action::STRING, s.V:detail::STRING, "
                    "  OBJECT_DELETE(s.V, 'id'))",
                    (client.to_json(new_events),),
                )
            if meta_changed:
                cursor.execute(
                    "MERGE INTO SECURITY.STORE_SETTINGS t USING (SELECT 'store_meta' AS K, PARSE_JSON(%s) AS V) s "
                    "ON t.SETTING_KEY = s.K "
                    "WHEN MATCHED THEN UPDATE SET SETTING_VALUE = s.V, UPDATED_AT = CURRENT_TIMESTAMP() "
                    "WHEN NOT MATCHED THEN INSERT (SETTING_KEY, SETTING_VALUE) VALUES (s.K, s.V)",
                    (client.to_json(meta),),
                )

        snapshot = copy.deepcopy(baseline)
        users = {user.get("username"): user for user in snapshot.get("users", [])}
        users.update({user["username"]: copy.deepcopy(user) for user in changed_users})
        snapshot["users"] = sorted(users.values(), key=lambda user: str(user.get("username")))
        snapshot.setdefault("role_permissions", {}).update(copy.deepcopy(changed_perms))
        snapshot["audit"] = (snapshot.get("audit", []) + [
            {k: v for k, v in event.items() if k != "id"} for event in new_events
        ])[-AUDIT_WINDOW:]
        snapshot.update(copy.deepcopy(meta))
        _SNAPSHOT, _LOADED_AT = snapshot, time.monotonic()


def invalidate() -> None:
    global _SNAPSHOT
    with _LOCK:
        _SNAPSHOT = None
