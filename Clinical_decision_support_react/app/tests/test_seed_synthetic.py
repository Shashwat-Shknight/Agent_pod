"""The synthetic seeder runs the real workflow on temporary stores, fills every stage, and removes cleanly."""

from __future__ import annotations

import shutil

import pytest

from app.scripts import seed_synthetic as seeder
from app.scripts.synthetic_scenarios import SCENARIOS
from app.services import ehr_json_service
from app.web import clinical_routes, ehr, records


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.delenv("NUUCARE_AUTH_DISABLED", raising=False)
    store_copy = tmp_path / "ehr.json"
    shutil.copy(ehr_json_service.JSON_PATH, store_copy)
    monkeypatch.setattr(ehr_json_service, "JSON_PATH", store_copy)
    monkeypatch.setattr("app.web.context.ACCESS_STORE_PATH", tmp_path / "access.json")
    monkeypatch.setattr("app.web.admin_routes.ACCESS_STORE_PATH", tmp_path / "access.json")
    monkeypatch.setattr(records, "DB_FILE", tmp_path / "records.json")
    monkeypatch.setattr("app.domain.settings.APPROVED_SOAP_DIR", tmp_path / "soap")
    monkeypatch.setattr(clinical_routes, "APPROVED_SOAP_DIR", tmp_path / "soap")
    monkeypatch.setattr(clinical_routes, "_agent", clinical_routes._agent)  # the seeder swaps it; restore afterwards
    ehr.invalidate()
    yield tmp_path
    ehr.invalidate()


def test_scenarios_are_internally_consistent():
    keys = [s.key for s in SCENARIOS]
    assert len(keys) == len(set(keys))
    for s in SCENARIOS:
        assert "Plan:" in s.soap() and "**Recommendation**" in s.recommendation_text()
        assert len(s.transcript) >= clinical_routes.MIN_TRANSCRIPT_CHARS
        assert s.passages and s.recommendations and s.problems


def test_seed_fills_every_stage_then_removes_everything(stores):
    before = len(ehr_json_service.list_patients())
    result = seeder.seed(patient_count=12, consultation_count=12, password="Synthetic9x", log=lambda *_: None)
    assert result["failures"] == []
    assert result["patients"] == 12 and result["consultations"] == 12

    patients = [p for p in ehr_json_service.list_patients() if seeder.is_synthetic_patient(p["patient_id"])]
    assert len(patients) == 12 and len(ehr_json_service.list_patients()) == before + 12
    chart = ehr_json_service.get_patient_chart(patients[0]["patient_id"])
    assert {"Problems", "Insurance", "Vitals", "Encounters", "Clinical_Notes"} <= set(chart["records"])

    seeded = [r for r in records.list_records() if r.get("synthetic")]
    assert len(seeded) == 12
    signed = [r for r in seeded if r.get("status") == "Approved"]
    billed = [r for r in seeded if r.get("bill_generated_at")]
    coded = [r for r in seeded if r.get("code_decisions")]
    drafts = [r for r in seeded if r.get("status") != "Approved"]
    assert drafts and signed and coded and billed
    assert all(r.get("version", 0) >= 1 for r in seeded)
    assert all("•" in r["summary"] for r in signed)  # the plan came from CDS ("push to plan")
    assert len({r["date"] for r in seeded}) > 1  # spread over several days

    # A second run refuses to duplicate; --reset rebuilds; --remove leaves nothing synthetic behind.
    with pytest.raises(RuntimeError, match="already exist"):
        seeder.seed(patient_count=3, consultation_count=0, password="Synthetic9x", log=lambda *_: None)
    removed = seeder.remove_synthetic(log=lambda *_: None)
    assert removed == {"consultations": 12, "patients": 12, "users": len(seeder.TEAM)}
    assert len(ehr_json_service.list_patients()) == before
    assert not [r for r in records.list_records() if r.get("synthetic")]


def test_seeded_users_can_sign_in_and_respect_roles(stores):
    seeder.seed(patient_count=2, consultation_count=0, password="Synthetic9x", log=lambda *_: None)
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())
    h = {"X-NuuCare-Client": "web"}
    assert client.post("/api/auth/login", headers=h, json={"username": "syn.doctor", "password": "Synthetic9x"}).json()["state"] == "ready"
    assert client.get("/api/coding/queue").status_code == 403  # segregation of duties holds for seeded doctors too
    assert client.get("/api/patients?q=P2000").json()["total"] == 2
