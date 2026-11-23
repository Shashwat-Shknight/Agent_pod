"""Live Snowflake round-trip tests. Opt in with:  SNOWFLAKE_INTEGRATION=1 pytest app/tests/test_snowflake_integration.py

They use the configured .env service user and write only rows with an ITEST- prefix, which
are removed at the end.
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("SNOWFLAKE_INTEGRATION") != "1", reason="set SNOWFLAKE_INTEGRATION=1")


@pytest.fixture(autouse=True)
def _snowflake_backend(monkeypatch):
    from app.db import settings

    monkeypatch.setenv("STORAGE_BACKEND", "snowflake")
    settings.reset_cache()
    yield
    settings.reset_cache()


def test_encounter_round_trip_and_child_tables():
    from app.db import client, encounters

    encounter_id = f"ITEST-{uuid.uuid4().hex[:8]}"
    record = {
        "id": encounter_id, "name": "Integration Patient", "doctor": "Dr Test", "status": "Pending",
        "date": "19/11/2026", "time": "10:30", "transcript": "Doctor: hello", "summary": "Subjective: test",
        "code_results": {"icd10": [{"ICD-10 Code": "R05.9", "Extracted Condition": "cough"}],
                         "cpt": [{"CPT/HCPCS Code": "71046", "Extracted Procedure": "chest x-ray"}]},
        "code_decisions": {"CPT:71046": {"status": "Accepted", "by": "coder"}},
    }
    try:
        assert encounters.save_encounter(record, "itest") == "saved"
        encounters._KNOWN_HASH.pop(encounter_id, None)
        assert encounters.save_encounter(record, "itest") == "unchanged"  # server-side hash check
        record["status"] = "Approved"
        assert encounters.save_encounter(record, "itest") == "saved"
        assert encounters.get_encounter(encounter_id)["status"] == "Approved"
        codes = client.query(
            "SELECT CODE_SYSTEM, CODE, DECISION_STATUS FROM CLINICAL.ENCOUNTER_CODES WHERE ENCOUNTER_ID = %s ORDER BY 1",
            (encounter_id,))
        assert [(row["CODE_SYSTEM"], row["CODE"], row["DECISION_STATUS"]) for row in codes] == [
            ("CPT", "71046", "Accepted"), ("ICD10", "R05.9", None)]
        assert [row["version"] for row in encounters.history(encounter_id)] == [2, 1]
        row = client.query("SELECT ENCOUNTER_AT::STRING AS T FROM CLINICAL.ENCOUNTERS WHERE ENCOUNTER_ID = %s",
                           (encounter_id,))[0]
        assert row["T"].startswith("2026-11-19 10:30")
    finally:
        for table in ("ENCOUNTER_CODES", "ENCOUNTER_AI_OUTPUTS", "ENCOUNTER_TRANSCRIPTS", "ENCOUNTER_HISTORY", "ENCOUNTERS"):
            client.execute(f"DELETE FROM CLINICAL.{table} WHERE ENCOUNTER_ID = %s", (encounter_id,))


def test_soap_note_versions():
    from app.db import client, soap_notes

    encounter_id = f"ITEST-{uuid.uuid4().hex[:8]}"
    try:
        soap_notes.save_approved({"encounter_id": encounter_id, "patient_id": "P", "soap_note": "S: x",
                                  "approved_timestamp": "2026-11-19T10:00:00+05:30"}, actor="itest")
        assert soap_notes.load_approved(encounter_id)["soap_note"] == "S: x"
        assert soap_notes.withdraw(encounter_id, actor="itest") is True
        assert soap_notes.load_approved(encounter_id) is None
        assert soap_notes.withdraw(encounter_id, actor="itest") is False
    finally:
        client.execute("DELETE FROM CLINICAL.SOAP_NOTES WHERE ENCOUNTER_ID = %s", (encounter_id,))


def test_pdf_archive_round_trip_and_dedupe():
    from app.db import billing, client

    record_id = f"ITEST-{uuid.uuid4().hex[:8]}"
    content = b"%PDF-1.4 integration test " + record_id.encode()
    try:
        first = billing.archive_document("DRAFT_BILL", "bill.pdf", content, record_id=record_id, actor="itest")
        again = billing.archive_document("DRAFT_BILL", "bill.pdf", content, record_id=record_id, actor="itest")
        assert first["created"] and not again["created"] and first["document_id"] == again["document_id"]
        assert billing.get_document(first["document_id"]) == ("bill.pdf", content)
        assert billing.list_documents(record_id=record_id)[0]["size_bytes"] == len(content)
    finally:
        client.execute("DELETE FROM BILLING.BILL_DOCUMENTS WHERE RECORD_ID = %s", (record_id,))


def test_vbc_state_preserves_exact_text():
    from app.db import client, vbc_state

    key = f"ITEST-{uuid.uuid4().hex[:8]}"
    state = {"revision": 3, "ratio": 1.0, "nested": {"b": 2, "a": 1}}
    try:
        vbc_state.save(key, state)
        loaded = vbc_state.load(key)
        assert loaded == state and list(loaded["nested"]) == ["b", "a"] and isinstance(loaded["ratio"], float)
    finally:
        client.execute("DELETE FROM VBC.STATE_DOCUMENTS WHERE DOC_KEY = %s", (key,))
