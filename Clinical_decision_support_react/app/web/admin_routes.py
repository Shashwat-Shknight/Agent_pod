"""Sign-in, dashboards, oversight, user access and knowledge-base search."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.domain import access_control as ac
from app.domain.admin_service import ACTIONS, CLINICAL_MODULES, CLINICAL_ROLES, ROLES
from app.domain.dashboard import filter_records
from app.domain.oversight import build_admin_workflow_rows
from app.domain.settings import ACCESS_STORE_PATH
from app.web import ehr, records
from app.web.clinical_routes import record_summary
from app.web.context import Caller, caller_allowing_password_change, current_caller, requires, store_transaction
from app.web.errors import ApiError
from app.web.sessions import ABSOLUTE_LIFETIME, COOKIE_NAME, SESSIONS

router = APIRouter(prefix="/api", tags=["web"])

# React route -> permission module (mirrors access_control.PAGE_MODULES for the screens React implements)
PAGES = {
    "dashboard": "Clinical Overview",
    "consultations": "Clinical Review",
    "review": "Clinical Review",
    "knowledge": "Clinical Review",
    "coding": "Codes",
    "billing": "RCM Workspace",
    "oversight": "Admin Dashboard",
    "users": "Super Admin",
}


def _profile(user: dict[str, Any], store: dict[str, Any]) -> dict[str, Any]:
    permissions = {module: {action: ac.can(store, user, module, action) for action in ACTIONS} for module in CLINICAL_MODULES}
    return {
        "username": user["username"],
        "name": user.get("name") or user["username"],
        "email": user.get("email", ""),
        "role": user.get("role"),
        "must_change_password": bool(user.get("must_change_password")),
        "permissions": permissions,
        "pages": [page for page, module in PAGES.items() if permissions.get(module, {}).get("View")],
    }


def _set_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
        max_age=int(ABSOLUTE_LIFETIME.total_seconds()), path="/",
    )


# ---------------------------------------------------------------- auth
@router.get("/auth/session")
def session_state(request: Request):
    """Never 401: tells the client whether to show bootstrap, sign-in, password change or the app."""
    store = ac.load_store(ACCESS_STORE_PATH)
    if ac.needs_bootstrap(store):
        return {"state": "bootstrap", "user": None}
    session, reason = SESSIONS.resolve(request.cookies.get(COOKIE_NAME))
    user = ac.get_user(store, session.username) if session else None
    if user is None or not user.get("active", True):
        return {"state": "signed_out", "user": None, "notice": reason or ""}
    profile = _profile(ac.public_user(user), store)
    return {"state": "password_change" if profile["must_change_password"] else "ready", "user": profile}


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.post("/auth/login")
def login(body: Credentials, request: Request, response: Response):
    with store_transaction() as store:
        if ac.needs_bootstrap(store):
            raise ApiError(409, "bootstrap_required", "Create the first administrator account first.")
        try:
            user = ac.authenticate(store, body.username, body.password)
            failure = None
        except ac.AuthError as error:
            user, failure = None, str(error)
    if failure:  # the store was still saved: failed attempts and lockouts must persist
        raise ApiError(401, "invalid_credentials", failure)
    session = SESSIONS.create(user["username"])
    _set_cookie(response, request, session.token)
    profile = _profile(ac.public_user(user), ac.load_store(ACCESS_STORE_PATH))
    return {"state": "password_change" if profile["must_change_password"] else "ready", "user": profile}


class Bootstrap(BaseModel):
    username: str
    name: str
    email: str = ""
    password: str


@router.post("/auth/bootstrap")
def bootstrap(body: Bootstrap, request: Request, response: Response):
    with store_transaction() as store:
        if not ac.needs_bootstrap(store):  # re-checked under the lock: two first visitors cannot both become admin
            raise ApiError(409, "already_bootstrapped", "An administrator already exists. Sign in instead.")
        try:
            user = ac.create_user(store, body.username, body.name, body.email, ac.SUPER_ADMIN, body.password, actor="bootstrap")
        except ValueError as error:
            raise ApiError(422, "invalid_user", str(error)) from error
        user["last_login"] = datetime.now().strftime(ac.TIME_FORMAT)
        snapshot = dict(store)
    session = SESSIONS.create(user["username"])
    _set_cookie(response, request, session.token)
    return {"state": "ready", "user": _profile(ac.public_user(user), snapshot)}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    session, _ = SESSIONS.resolve(token)
    if session:
        with store_transaction() as store:
            ac.audit(store, session.username, "auth.sign_out")
    SESSIONS.drop(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"state": "signed_out"}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/password")
def change_password(body: PasswordChange, caller: Caller = Depends(caller_allowing_password_change)):
    with store_transaction() as store:
        user = ac.get_user(store, caller.username)
        if not ac.verify_password(body.current_password, user.get("password_hash", "")):
            raise ApiError(422, "wrong_password", "The current password is not correct.")
        try:
            ac.set_password(store, caller.username, body.new_password, actor=caller.username)
        except ValueError as error:
            raise ApiError(422, "weak_password", str(error)) from error
        snapshot = dict(store)
    SESSIONS.drop_user(caller.username, keep=caller.token)  # sign out other devices
    return {"state": "ready", "user": _profile(ac.public_user(ac.get_user(snapshot, caller.username)), snapshot)}


# ---------------------------------------------------------------- dashboards
@router.get("/dashboard")
def dashboard(mine: bool = False, status: str = "All Statuses", window: str = "All Time", q: str = "",
              caller: Caller = Depends(requires("Clinical Overview"))):
    all_records = records.list_records()
    today = datetime.now().date()
    filtered = filter_records(all_records, q.strip().lower(), window, status,
                              assignee=caller.username if mine else None, today=today)
    approved = sum(r.get("status") == "Approved" for r in filtered)
    coded = sum(bool((r.get("code_results") or {}).get("cpt") or (r.get("code_results") or {}).get("icd10")) for r in filtered)
    daily: Counter = Counter()
    signed: Counter = Counter()
    for record in filtered:
        daily[record.get("date", "")] += 1
        if record.get("status") == "Approved":
            signed[record.get("approval_date") or record.get("date", "")] += 1

    def iso(value):
        try:
            return datetime.strptime(value, "%d/%m/%Y").date().isoformat()
        except (TypeError, ValueError):
            return None

    days = sorted({d for d in (iso(k) for k in list(daily) + list(signed)) if d})
    by_iso_daily = {iso(k): v for k, v in daily.items()}
    by_iso_signed = {iso(k): v for k, v in signed.items()}
    try:
        patients = len(ehr.patients())
        ehr_status = {"connected": True, "patients": patients}
    except Exception as error:  # noqa: BLE001 - the dashboard still renders without the EHR
        ehr_status = {"connected": False, "error": str(error)}
    return {
        "kpis": {
            "total": len(filtered),
            "today": sum(r.get("date") == today.strftime("%d/%m/%Y") for r in filtered),
            "pending": sum(r.get("status") == "Pending" for r in filtered),
            "signed": approved,
            "coded": coded,
            "sign_off_rate": round(approved / len(filtered) * 100) if filtered else 0,
        },
        "activity": [{"date": d, "consults": by_iso_daily.get(d, 0), "signed": by_iso_signed.get(d, 0)} for d in days[-30:]],
        "queue": [record_summary(r) for r in filtered],
        "ehr": ehr_status,
    }


@router.get("/oversight")
def oversight(caller: Caller = Depends(requires("Admin Dashboard"))):
    names = {user["username"]: user.get("name", user["username"]) for user in caller.store["users"]}
    try:
        export, ehr_ok = ehr.export(), True
    except Exception:  # noqa: BLE001 - show consultations even when the EHR is unreachable
        export, ehr_ok = {}, False
    rows = build_admin_workflow_rows(records.list_records(), export, names)
    out = []
    for row in rows:
        item = {key: value for key, value in row.items() if not key.startswith("_")}
        item["Updated"] = row["Updated"].isoformat(timespec="minutes") if row["Updated"] else ""
        item["record_id"] = (row.get("_record") or {}).get("id")
        item["version"] = records.version_of(row.get("_record") or {})
        out.append(item)
    users = [{"username": u["username"], "name": u.get("name", ""), "role": u.get("role")} for u in caller.store["users"]
             if u.get("active", True)]
    return {"rows": out, "ehr_connected": ehr_ok, "users": users}


# ---------------------------------------------------------------- user access
def _user_row(user: dict[str, Any]) -> dict[str, Any]:
    public = ac.public_user(user)
    locked = user.get("locked_until") and datetime.now() < datetime.strptime(user["locked_until"], ac.TIME_FORMAT)
    return {key: public.get(key) for key in ("username", "name", "email", "role", "active", "out_of_office", "fallback",
                                              "must_change_password", "last_login", "created_at")} | {"locked": bool(locked)}


@router.get("/admin/users")
def list_users(caller: Caller = Depends(requires("Super Admin"))):
    return {"users": [_user_row(u) for u in caller.store["users"]], "roles": list(ROLES), "clinical_roles": list(CLINICAL_ROLES)}


class NewUser(BaseModel):
    username: str
    name: str
    email: str = ""
    role: str
    password: str


@router.post("/admin/users")
def create_user(body: NewUser, caller: Caller = Depends(requires("Super Admin", "Edit"))):
    with store_transaction() as store:
        try:
            ac.create_user(store, body.username, body.name, body.email, body.role, body.password,
                           actor=caller.username, must_change_password=True)
        except ValueError as error:
            raise ApiError(422, "invalid_user", str(error)) from error
        users = [_user_row(u) for u in store["users"]]
    return {"users": users}


class UserUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    role: str | None = None
    active: bool | None = None
    out_of_office: bool | None = None


@router.patch("/admin/users/{username}")
def update_user(username: str, body: UserUpdate, caller: Caller = Depends(requires("Super Admin", "Edit"))):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if username == caller.username and (changes.get("active") is False or ("role" in changes and changes["role"] != caller.user.get("role"))):
        raise ApiError(422, "self_lockout", "You cannot deactivate yourself or change your own role.")
    with store_transaction() as store:
        try:
            ac.update_user(store, username, caller.username, **changes)
        except ValueError as error:
            raise ApiError(422, "invalid_user", str(error)) from error
        users = [_user_row(u) for u in store["users"]]
    if changes.get("active") is False or "role" in changes:
        SESSIONS.drop_user(username)  # deactivated or re-roled users sign in again with the new rights
    return {"users": users}


class PasswordReset(BaseModel):
    password: str


@router.post("/admin/users/{username}/password")
def reset_password(username: str, body: PasswordReset, caller: Caller = Depends(requires("Super Admin", "Edit"))):
    with store_transaction() as store:
        try:
            ac.set_password(store, username, body.password, actor=caller.username, temporary=True)
        except ValueError as error:
            raise ApiError(422, "weak_password", str(error)) from error
        users = [_user_row(u) for u in store["users"]]
    SESSIONS.drop_user(username)
    return {"users": users}


@router.post("/admin/users/{username}/unlock")
def unlock(username: str, caller: Caller = Depends(requires("Super Admin", "Edit"))):
    with store_transaction() as store:
        if ac.get_user(store, username) is None:
            raise ApiError(404, "not_found", f"Unknown user {username}.")
        ac.unlock_user(store, username, caller.username)
        users = [_user_row(u) for u in store["users"]]
    return {"users": users}


@router.get("/admin/roles")
def role_matrix(caller: Caller = Depends(requires("Super Admin"))):
    return {
        "roles": list(CLINICAL_ROLES),
        "modules": list(CLINICAL_MODULES),
        "actions": list(ACTIONS),
        "matrix": {role: {m: caller.store["role_permissions"].get(role, {}).get(m, {}) for m in CLINICAL_MODULES}
                   for role in CLINICAL_ROLES},
        "locked": {"Super Admin": "Always full access", "Clinical Reviewer · Codes": "Segregation of duties: clinicians never code"},
    }


class RoleUpdate(BaseModel):
    rows: list[dict[str, Any]]


@router.put("/admin/roles/{role}")
def save_role(role: str, body: RoleUpdate, caller: Caller = Depends(requires("Super Admin", "Admin"))):
    with store_transaction() as store:
        try:
            ac.save_role_permissions(store, role, body.rows, caller.username)
        except ValueError as error:
            raise ApiError(422, "invalid_role", str(error)) from error
    return role_matrix(caller=Caller(user=caller.user, store=ac.load_store(ACCESS_STORE_PATH), token=caller.token))


@router.get("/admin/audit")
def audit_log(limit: int = 200, caller: Caller = Depends(requires("Super Admin"))):
    return {"events": list(reversed(caller.store.get("audit", [])))[: max(1, min(limit, 2000))]}


# ---------------------------------------------------------------- knowledge base
@router.get("/kb/search")
def kb_search(q: str, caller: Caller = Depends(requires("Clinical Review"))):
    query = q.strip()
    if len(query) < 3:
        raise ApiError(422, "query_too_short", "Type at least 3 characters.")
    from app.rag.retriever import retrieve_chunks

    try:
        chunks = retrieve_chunks(query[:300], results=8)
    except FileNotFoundError as error:
        raise ApiError(503, "knowledge_base_missing", str(error)) from error
    return {"results": [{"text": c["text"], "source": c["metadata"].get("source", ""),
                         "chunk": c["metadata"].get("chunk"), "score": round(1 - float(c.get("distance", 1)), 3)} for c in chunks]}


@router.get("/me")
def me(caller: Caller = Depends(current_caller)):
    return _profile(caller.user, caller.store)

