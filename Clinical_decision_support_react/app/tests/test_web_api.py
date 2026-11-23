"""React API: authentication, permissions, concurrency and the full encounter hand-off, with edge cases.

The LLM is replaced by a deterministic fake; everything else (coding engine, guardrails, EHR store) is real,
running on temporary copies of the JSON stores.
"""

from __future__ import annotations

import io
import shutil
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from app.agent.context import EncounterContext
from app.services import ehr_json_service
from app.web import clinical_routes, ehr, records
from app.web.sessions import SESSIONS

H = {"X-NuuCare-Client": "web"}
PASSWORD = "Passw0rd!x"
TRANSCRIPT = (
    "Doctor: Good morning. What brings you in?\n"
    "Patient: My sugars have been running high and I have burning in both feet.\n"
    "Doctor: Blood pressure today is 152 over 88. We collected blood for an A1c and lipid panel today.\n"
    "Doctor: Assessment: type 2 diabetes, uncontrolled with high sugars. Diabetic peripheral neuropathy. Hypertension."
)
SOAP = (
    "Subjective:\nHigh sugars, burning feet.\n\nObjective:\nBP 152/88. Blood collected for A1c and lipid panel.\n\n"
    "Assessment:\n1) Type 2 diabetes, uncontrolled. 2) Diabetic neuropathy. 3) Hypertension.\n\nPlan:\nNot documented."
)


class FakeAgent:
    calls = 0

    def prepare(self, transcript):
        return EncounterContext(transcript=transcript, soap_note=SOAP, patient_summary=SOAP,
                                assessment="1) Type 2 diabetes. 2) Neuropathy. 3) Hypertension.", key_highlights=["High sugars"])

    def add_clinical_decision_support(self, context):
        FakeAgent.calls += 1
        context.retrieved_guidelines = "Source: Harrison's Principles of Internal Medicine, passage 9 · Topic: Hypertension\nThiazides lower BP."
        context.recommendations = (
            "**Recommendation**\n1. **Restart metformin**: titrate.\n2. **Add a thiazide**: to lisinopril.\n\n"
            "**Reasoning**\nGrounded.\n\n**Safety Note**\nNo penicillin."
        )
        return context


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("NUUCARE_AUTH_DISABLED", raising=False)
    store_copy = tmp_path / "ehr.json"
    shutil.copy(ehr_json_service.JSON_PATH, store_copy)
    monkeypatch.setattr(ehr_json_service, "JSON_PATH", store_copy)
    monkeypatch.setattr("app.web.context.ACCESS_STORE_PATH", tmp_path / "access.json")
    monkeypatch.setattr("app.web.admin_routes.ACCESS_STORE_PATH", tmp_path / "access.json")
    monkeypatch.setattr(records, "DB_FILE", tmp_path / "records.json")
    monkeypatch.setattr("app.domain.settings.APPROVED_SOAP_DIR", tmp_path / "soap")
    monkeypatch.setattr(clinical_routes, "APPROVED_SOAP_DIR", tmp_path / "soap")
    monkeypatch.setattr(clinical_routes, "_agent", lambda: FakeAgent())
    ehr.invalidate()
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    ehr.invalidate()


def _bootstrap(client):
    r = client.post("/api/auth/bootstrap", headers=H, json={"username": "admin", "name": "Admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r


def _user(client, username, role):
    assert client.post("/api/admin/users", headers=H, json={"username": username, "name": username.title(), "role": role,
                                                           "password": PASSWORD}).status_code == 200


def _login(client, username, password=PASSWORD):
    client.cookies.clear()
    r = client.post("/api/auth/login", headers=H, json={"username": username, "password": password})
    if r.status_code == 200 and r.json()["state"] == "password_change":
        assert client.post("/api/auth/password", headers=H, json={"current_password": password,
                                                                 "new_password": password + "9"}).status_code == 200
    return r


def _setup_team(client):
    _bootstrap(client)
    _user(client, "dr.carter", "Clinical Reviewer")
    _user(client, "coder.lee", "Coding Specialist")
    _user(client, "rcm.patel", "RCM Analyst")
    for name in ("dr.carter", "coder.lee", "rcm.patel"):
        _login(client, name)
    return PASSWORD + "9"


def _patient_id(client):
    return client.get("/api/patients?q=P1000").json()["patients"][0]["patient_id"]


def _consultation(client):
    draft = client.post("/api/scribe/text", headers=H, json={"transcript": TRANSCRIPT})
    assert draft.status_code == 200, draft.text
    created = client.post("/api/consultations", headers=H, json={"draft_id": draft.json()["draft_id"], "patient_id": _patient_id(client)})
    assert created.status_code == 200, created.text
    return created.json()["id"], draft.json()["draft_id"]


# ---------------------------------------------------------------- auth & sessions
def test_session_state_drives_bootstrap_then_sign_in(api):
    assert api.get("/api/auth/session").json()["state"] == "bootstrap"
    assert api.post("/api/auth/login", headers=H, json={"username": "x", "password": "y"}).json()["error"]["code"] == "bootstrap_required"
    _bootstrap(api)
    assert api.get("/api/auth/session").json()["state"] == "ready"
    # a second bootstrap attempt (e.g. another first visitor) is refused
    assert api.post("/api/auth/bootstrap", headers=H, json={"username": "eve", "name": "Eve", "password": PASSWORD}).status_code == 409


def test_unauthenticated_and_csrf_requests_are_refused(api):
    _bootstrap(api)
    assert api.post("/api/auth/logout", json={}).json()["error"]["code"] == "csrf_blocked"
    api.post("/api/auth/logout", headers=H)
    assert api.get("/api/dashboard").json()["error"]["code"] == "unauthenticated"
    # the original endpoints are no longer open on the network, except the liveness probe
    assert api.get("/patients").status_code == 401
    assert api.get("/health").status_code == 200


def test_wrong_password_locks_account_and_new_users_must_change_password(api):
    _bootstrap(api)
    _user(api, "dr.carter", "Clinical Reviewer")
    for _ in range(5):
        assert _login(api, "dr.carter", "wrong-password1").status_code == 401
    assert "locked" in _login(api, "dr.carter").json()["error"]["message"].lower()
    _login(api, "admin")
    assert api.post("/api/admin/users/dr.carter/unlock", headers=H).status_code == 200
    api.cookies.clear()
    first = api.post("/api/auth/login", headers=H, json={"username": "dr.carter", "password": PASSWORD})
    assert first.json()["state"] == "password_change"
    assert api.get("/api/dashboard").json()["error"]["code"] == "password_change_required"


def test_deactivated_user_is_signed_out_immediately(api):
    _bootstrap(api)
    _user(api, "dr.carter", "Clinical Reviewer")
    _login(api, "dr.carter")
    doctor_cookie = dict(api.cookies)
    _login(api, "admin")
    assert api.patch("/api/admin/users/dr.carter", headers=H, json={"active": False}).status_code == 200
    api.cookies.clear()
    api.cookies.update(doctor_cookie)
    assert api.get("/api/dashboard").status_code == 401


def test_admin_cannot_lock_themselves_out(api):
    _bootstrap(api)
    assert api.patch("/api/admin/users/admin", headers=H, json={"active": False}).json()["error"]["code"] == "self_lockout"


# ---------------------------------------------------------------- segregation of duties
def test_clinicians_never_reach_codes_or_billing(api):
    _setup_team(api)
    _login(api, "dr.carter", PASSWORD + "9")
    record_id, _ = _consultation(api)
    view = api.get(f"/api/consultations/{record_id}").json()
    assert not {"code_results", "code_decisions", "coding_audit", "bill_estimate"} & set(view["record"])
    assert api.get(f"/api/coding/{record_id}").status_code == 403
    assert api.get(f"/api/billing/{record_id}").status_code == 403
    assert "coding" not in api.get("/api/auth/session").json()["user"]["pages"]
    # a role-matrix edit cannot grant codes to a clinician
    _login(api, "admin")
    api.put("/api/admin/roles/Clinical Reviewer", headers=H, json={"rows": [{"Module": "Codes", "View": True, "Edit": True}]})
    _login(api, "dr.carter", PASSWORD + "9")
    assert api.get(f"/api/coding/{record_id}").status_code == 403


# ---------------------------------------------------------------- scribe & consultation edge cases
def test_scribe_rejects_empty_and_bad_audio(api):
    _setup_team(api)
    _login(api, "dr.carter", PASSWORD + "9")
    assert api.post("/api/scribe/text", headers=H, json={"transcript": "hi"}).json()["error"]["code"] == "transcript_too_short"
    bad = api.post("/api/scribe/audio", headers=H, files={"file": ("a.webm", b"\x1aE\xdf\xa3" + b"0" * 100, "audio/webm")})
    assert bad.status_code == 415
    assert api.post("/api/scribe/audio", headers=H, files={"file": ("a.wav", b"", "audio/wav")}).json()["error"]["code"] == "audio_empty"


def test_silent_wav_reports_no_speech_or_unreachable_service(api, monkeypatch):
    _setup_team(api)
    _login(api, "dr.carter", PASSWORD + "9")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1), handle.setsampwidth(2), handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", 0) * 1600)
    import speech_recognition as sr

    monkeypatch.setattr(sr.Recognizer, "recognize_google", lambda self, audio: (_ for _ in ()).throw(sr.UnknownValueError()))
    r = api.post("/api/scribe/audio", headers=H, files={"file": ("a.wav", buffer.getvalue(), "audio/wav")})
    assert r.json()["error"]["code"] == "no_speech"


def test_create_is_idempotent_and_drafts_are_private(api):
    _setup_team(api)
    _login(api, "dr.carter", PASSWORD + "9")
    record_id, draft_id = _consultation(api)
    again = api.post("/api/consultations", headers=H, json={"draft_id": draft_id, "patient_id": _patient_id(api)})
    assert again.json() == {"id": record_id, "warnings": [], "duplicate": True}
    assert api.post("/api/consultations", headers=H, json={"draft_id": "nope", "patient_id": "x"}).status_code == 410
    _login(api, "admin")
    assert api.post("/api/consultations", headers=H, json={"draft_id": draft_id, "patient_id": _patient_id(api)}).status_code == 403


def test_cds_failure_still_saves_the_consultation(api, monkeypatch):
    _setup_team(api)
    _login(api, "dr.carter", PASSWORD + "9")

    class Broken(FakeAgent):
        def add_clinical_decision_support(self, context):
            raise ConnectionError("gateway down")

    monkeypatch.setattr(clinical_routes, "_agent", lambda: Broken())
    draft = api.post("/api/scribe/text", headers=H, json={"transcript": TRANSCRIPT}).json()
    created = api.post("/api/consultations", headers=H, json={"draft_id": draft["draft_id"], "patient_id": _patient_id(api)}).json()
    assert created["warnings"] and "Regenerate CDS" in created["warnings"][0]
    assert api.get(f"/api/consultations/{created['id']}").json()["cds"]["items"] == []


def test_stale_version_conflict_and_signed_note_lock(api):
    _setup_team(api)
    _login(api, "dr.carter", PASSWORD + "9")
    record_id, _ = _consultation(api)
    v = api.get(f"/api/consultations/{record_id}").json()["summary"]["version"]
    pushed = api.post(f"/api/consultations/{record_id}/plan", headers=H, json={"version": v, "line": "Restart metformin"})
    assert pushed.status_code == 200 and "Restart metformin" in pushed.json()["record"]["summary"]
    # a second tab still holding the old version is told to reload instead of overwriting
    assert api.put(f"/api/consultations/{record_id}/note", headers=H, json={"version": v, "summary": SOAP}).json()["error"]["code"] == "stale_record"
    v2 = pushed.json()["summary"]["version"]
    dup = api.post(f"/api/consultations/{record_id}/plan", headers=H, json={"version": v2, "line": "Restart metformin"}).json()
    assert dup["record"]["summary"].count("Restart metformin") == 1 and dup["warnings"]
    v3 = dup["summary"]["version"]
    assert api.post(f"/api/consultations/{record_id}/sign", headers=H,
                    json={"version": v3, "summary": "Subjective: x"}).json()["error"]["code"] == "soap_incomplete"
    signed = api.post(f"/api/consultations/{record_id}/sign", headers=H, json={"version": v3}).json()
    assert signed["record"]["status"] == "Approved" and signed["handoff"] == "Queued for coding"
    v4 = signed["summary"]["version"]
    assert api.put(f"/api/consultations/{record_id}/note", headers=H, json={"version": v4, "summary": SOAP}).json()["error"]["code"] == "note_signed"


def test_recommendation_override_needs_a_reason(api):
    _setup_team(api)
    _login(api, "dr.carter", PASSWORD + "9")
    record_id, _ = _consultation(api)
    v = api.get(f"/api/consultations/{record_id}").json()["summary"]["version"]
    assert api.post(f"/api/consultations/{record_id}/recommendations/0/decision", headers=H,
                    json={"version": v, "status": "overridden"}).json()["error"]["code"] == "note_required"
    ok = api.post(f"/api/consultations/{record_id}/recommendations/0/decision", headers=H,
                  json={"version": v, "status": "accepted"}).json()
    assert ok["cds"]["decisions"]["0"]["status"] == "accepted"
    assert api.post(f"/api/consultations/{record_id}/recommendations/9/decision", headers=H,
                    json={"version": ok["summary"]["version"], "status": "accepted"}).status_code == 404


# ---------------------------------------------------------------- coding → billing → RCM
def test_full_handoff_doctor_coder_rcm(api):
    _setup_team(api)
    pw = PASSWORD + "9"
    _login(api, "dr.carter", pw)
    record_id, _ = _consultation(api)
    v = api.get(f"/api/consultations/{record_id}").json()["summary"]["version"]

    _login(api, "rcm.patel", pw)
    early = api.get(f"/api/billing/{record_id}").json()
    assert "The clinical note is not signed yet." in early["blockers"]
    assert api.post(f"/api/billing/{record_id}/send", headers=H,
                    json={"version": early["summary"]["version"], "attested": True}).json()["error"]["code"] == "note_unsigned"

    _login(api, "dr.carter", pw)
    v = api.get(f"/api/consultations/{record_id}").json()["summary"]["version"]
    api.post(f"/api/consultations/{record_id}/sign", headers=H, json={"version": v})

    _login(api, "coder.lee", pw)
    coding = api.get(f"/api/coding/{record_id}").json()
    codes = {row["code"] for row in coding["rows"]}
    assert {"E11.42", "E11.65"} <= codes and "HCC 37" in coding["totals"]["hccs"]
    keys = [row["key"] for row in coding["rows"]]
    accepted = api.post(f"/api/coding/{record_id}/decisions", headers=H,
                        json={"version": coding["summary"]["version"], "keys": keys, "status": "Accepted"}).json()
    assert accepted["totals"]["accepted"] == len(keys)
    assert api.post(f"/api/coding/{record_id}/decisions", headers=H, json={
        "version": accepted["summary"]["version"], "keys": ["ICD:Z99.99"], "status": "Accepted"}).json()["error"]["code"] == "unknown_code"
    assert api.post(f"/api/billing/{record_id}/send", headers=H, json={"version": 1, "attested": True}).status_code == 403

    _login(api, "rcm.patel", pw)
    bill = api.get(f"/api/billing/{record_id}").json()
    assert bill["can_send"] and bill["total"] > 0
    assert api.post(f"/api/billing/{record_id}/send", headers=H,
                    json={"version": bill["summary"]["version"], "attested": False}).json()["error"]["code"] == "attestation_required"
    sent = api.post(f"/api/billing/{record_id}/send", headers=H, json={"version": bill["summary"]["version"], "attested": True}).json()
    assert sent["sent"]["by"] == "rcm.patel" and sent["summary"]["stage"] == "Billed"
    # double click / second tab: no duplicate claim
    assert api.post(f"/api/billing/{record_id}/send", headers=H,
                    json={"version": sent["summary"]["version"], "attested": True}).json()["error"]["code"] == "already_sent"
    _login(api, "coder.lee", pw)
    locked = api.get(f"/api/coding/{record_id}").json()
    assert api.post(f"/api/coding/{record_id}/decisions", headers=H, json={
        "version": locked["summary"]["version"], "keys": keys[:1], "status": "Rejected"}).json()["error"]["code"] == "already_billed"

    _login(api, "admin")
    rows = api.get("/api/oversight").json()["rows"]
    assert any(row["record_id"] == record_id and row["Stage"] == "Billed" for row in rows)
    assert api.get("/api/dashboard").json()["kpis"]["signed"] == 1


def test_validation_errors_use_the_common_envelope(api):
    _bootstrap(api)
    r = api.post("/api/patients", headers=H, json={"first_name": "A", "last_name": "B", "dob": "2999-01-01"})
    body = r.json()
    assert r.status_code == 422 and body["error"]["code"] == "validation_failed" and "dob" in body["error"]["fields"]


def test_unknown_api_path_is_json_404_not_the_react_page(api):
    assert api.get("/api/nope").status_code == 404
    assert api.get("/vbc/whatever-not-there").status_code == 404


def test_sessions_expire_after_idle_time(api):
    from datetime import datetime, timedelta

    _bootstrap(api)
    token = api.cookies.get("nuucare_session")
    session, _ = SESSIONS.resolve(token)
    session.last_seen = datetime.now() - timedelta(hours=2)
    r = api.get("/api/dashboard").json()
    assert r["error"]["code"] == "session_expired" and "inactivity" in r["error"]["message"]
