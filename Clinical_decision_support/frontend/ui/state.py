"""Session state, signed-in user context, navigation and consultation persistence."""

import json
from copy import deepcopy

import streamlit as st

from app.db import encounters as encounter_repo
from app.db.client import StorageError
from app.db.settings import use_snowflake
from frontend import access_control as ac
from frontend.encounter_pipeline import get_record_insights
from frontend.settings import ACCESS_STORE_PATH, DB_FILE

SESSION_DEFAULTS = {
    "page": "dashboard",
    "current_record": None,
    "dash_page": 1,
    # consultation wizard
    "consult_step": "patient",
    "consult_draft": {},
    "consult_audio_signature": None,
    "scribe_result": None,
    # clinical review
    "review_step": "cds",
    "soap_rev": 0,
    "insight_count": 1,
    "detected_insights": [],
    "last_record_id": None,
    "edit_summary": "",
    # coding
    "active_code": None,
    "code_results": {"icd10": [], "cpt": []},
    "code_record_id": None,
    "code_results_signature": None,
    "code_matcher_version": None,
    "code_generation_error": "",
    # billing and tools
    "billing_patient_key": None,
    "billing_record_id": None,
    "kb_results": None,
}


class _SessionAccess:
    """Signed-in user and access store for the current browser session.

    Kept in st.session_state rather than a module global: imported modules live for the
    whole server process, so a global would be shared by every concurrent session.
    The router fills it at the start of each run; can() and current_user() read it.
    """

    _KEY = "_access_context"

    def _context(self):
        if self._KEY not in st.session_state:
            st.session_state[self._KEY] = {"store": ac.empty_store(), "user": None}
        return st.session_state[self._KEY]

    def __getitem__(self, key):
        return self._context()[key]

    def __setitem__(self, key, value):
        self._context()[key] = value


ACCESS = _SessionAccess()


# ---------- Consultation records (Snowflake CLINICAL.ENCOUNTERS, or clinical_records.json offline) ----------
def _actor():
    user = ACCESS["user"] if "_access_context" in st.session_state else None
    return (user or {}).get("username") or "system"


def load_db():
    if use_snowflake():
        try:
            return encounter_repo.list_encounters()
        except StorageError as error:
            st.session_state["storage_error"] = str(error)
            return []
    if not DB_FILE.exists():
        return []
    try:
        with DB_FILE.open("r", encoding="utf-8") as file:
            payload = json.load(file)
            return payload.get("records", []) if isinstance(payload, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_db(records):
    if use_snowflake():
        _persist(lambda: encounter_repo.save_encounters(records, _actor()))
        return
    temp_path = DB_FILE.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump({"records": records}, file, indent=4, ensure_ascii=False)
    temp_path.replace(DB_FILE)


def save_record(record):
    for index, existing in enumerate(st.session_state.records):
        if existing.get("id") == record.get("id"):
            st.session_state.records[index] = record
            break
    if use_snowflake():
        _persist(lambda: encounter_repo.save_encounter(record, _actor()))
        return
    save_db(st.session_state.records)


def _persist(write):
    """Run a database write; on failure keep the session state and surface the error loudly."""
    try:
        write()
        st.session_state.pop("storage_error", None)
    except StorageError as error:
        st.session_state["storage_error"] = str(error)
        st.error(f"Could not save to the database: {error}", icon=":material/cloud_off:")


def init_session():
    """Seed every session key once; records are loaded from disk on first run."""
    if "records" not in st.session_state:
        st.session_state.records = load_db()
        if use_snowflake():
            _restore_billing_state()
    for key, value in SESSION_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = deepcopy(value)
    if st.session_state.get("storage_error"):
        st.error(f"Database unavailable - changes cannot be saved: {st.session_state['storage_error']}",
                 icon=":material/cloud_off:")


def _restore_billing_state():
    """Released bills and generated claims outlive the browser session (BILLING schema)."""
    from app.db import billing as billing_repo

    try:
        st.session_state.claim_generation_bills = billing_repo.load_generated_bills()
        claims, reports = billing_repo.load_claims()
        st.session_state.generated_claim_records = claims
        st.session_state.claim_validation_reports = reports
    except StorageError as error:
        st.session_state["storage_error"] = str(error)


# ---------- Signed-in user and permissions ----------
def current_user():
    return ACCESS["user"] or {"username": "", "name": "Signed out", "role": "", "active": False}


def can(module, action="View"):
    return ac.can(ACCESS["store"], ACCESS["user"], module, action)


def can_open(page):
    return ac.can_view_page(ACCESS["store"], ACCESS["user"], page)


def user_label(username):
    user = ac.get_user(ACCESS["store"], username)
    return user["name"] if user else (username or "Unassigned")


def persist_access():
    ac.save_store(ACCESS_STORE_PATH, ACCESS["store"])


def log_action(action, detail=""):
    """Record a user action in the access audit trail."""
    ac.audit(ACCESS["store"], current_user()["username"], action, detail)
    persist_access()


def sign_out(reason=None):
    user = ACCESS["user"]
    if user:
        ac.audit(ACCESS["store"], user["username"], "auth.sign_out", reason or "")
        persist_access()
    for key in ("auth_username", "auth_last_seen"):
        st.session_state.pop(key, None)
    st.session_state.current_record = None
    st.session_state.page = "dashboard"
    if reason:
        st.session_state.auth_notice = reason


# ---------- Navigation ----------
def go_to(page):
    st.session_state.page = page


def start_new_consultation():
    st.session_state.scribe_result = None
    st.session_state.consult_step = "patient"
    st.session_state.consult_draft = {}
    st.session_state.consult_audio_signature = None
    st.session_state.page = "new_consult"


def open_record(record, destination="review"):
    st.session_state.current_record = record
    st.session_state.detected_insights = get_record_insights(record)
    st.session_state.insight_count = 1
    if destination == "review":
        st.session_state.review_step = "cds"
    go_to(destination)


def open_billing(record):
    st.session_state.current_record = record
    st.session_state.billing_record_id = record.get("id")
    go_to("billing")


def set_edit_summary(text):
    """Replace the working SOAP text and force the editor widget to pick it up."""
    st.session_state.edit_summary = text
    st.session_state.soap_rev += 1
