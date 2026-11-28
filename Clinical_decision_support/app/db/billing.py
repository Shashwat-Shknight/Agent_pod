"""BILLING schema repository: released draft bills, claim drafts and PDF documents."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from app.db import client


# ---------- Draft bills released to RCM ----------
def save_generated_bill(bill_key: str, source: str, bill: dict[str, Any], actor: str = "") -> None:
    patient = bill.get("patient") if isinstance(bill.get("patient"), dict) else {}
    total = bill.get("gross_total", bill.get("total", bill.get("gross")))
    try:
        total = round(float(str(total).replace("$", "").replace(",", "")), 2) if total not in (None, "") else None
    except ValueError:
        total = None
    client.execute(
        "MERGE INTO BILLING.GENERATED_BILLS t USING ("
        "  SELECT %s AS BILL_KEY, %s AS SOURCE, %s AS ENCOUNTER_ID, %s AS PATIENT_ID, %s::NUMBER(14,2) AS GROSS_TOTAL, "
        "         PARSE_JSON(%s) AS BILL_JSON, %s AS ACTOR) s "
        "ON t.BILL_KEY = s.BILL_KEY "
        "WHEN MATCHED THEN UPDATE SET SOURCE = s.SOURCE, ENCOUNTER_ID = s.ENCOUNTER_ID, PATIENT_ID = s.PATIENT_ID, "
        "  GROSS_TOTAL = s.GROSS_TOTAL, BILL_JSON = s.BILL_JSON, UPDATED_AT = CURRENT_TIMESTAMP() "
        "WHEN NOT MATCHED THEN INSERT (BILL_KEY, SOURCE, ENCOUNTER_ID, PATIENT_ID, GROSS_TOTAL, BILL_JSON, CREATED_BY) "
        "  VALUES (s.BILL_KEY, s.SOURCE, s.ENCOUNTER_ID, s.PATIENT_ID, s.GROSS_TOTAL, s.BILL_JSON, s.ACTOR)",
        (bill_key, source, str(bill.get("encounter_id") or ""), str(patient.get("Patient ID") or ""),
         total, client.to_json(bill), actor or "system"),
    )


def load_generated_bills() -> dict[str, dict[str, Any]]:
    rows = client.query("SELECT BILL_KEY, SOURCE, BILL_JSON FROM BILLING.GENERATED_BILLS ORDER BY CREATED_AT")
    return {row["BILL_KEY"]: {"source": row["SOURCE"], "bill": client.variant(row["BILL_JSON"])} for row in rows}


# ---------- Claims ----------
def _amount(claim: dict[str, Any]) -> float | None:
    for key in ("Claim Amount", "Total Facility Amount", "Total Claim Amount"):
        try:
            if claim.get(key) not in (None, ""):
                return round(float(str(claim[key]).replace("$", "").replace(",", "")), 2)
        except ValueError:
            continue
    return None


def save_claim(claim: dict[str, Any], actor: str = "") -> None:
    info = claim.get("Patient Information") if isinstance(claim.get("Patient Information"), dict) else {}
    encounter_id, claim_type = str(claim.get("Encounter ID") or ""), str(claim.get("Claim Type") or "")
    with client.transaction() as cursor:
        # A regenerated claim replaces any earlier claim of the same type for the encounter.
        cursor.execute(
            "UPDATE BILLING.CLAIMS SET IS_DELETED = TRUE, UPDATED_AT = CURRENT_TIMESTAMP() "
            "WHERE ENCOUNTER_ID = %s AND CLAIM_TYPE = %s AND CLAIM_ID <> %s AND NOT IS_DELETED",
            (encounter_id, claim_type, str(claim["Claim ID"])),
        )
        cursor.execute(
            "MERGE INTO BILLING.CLAIMS t USING ("
            "  SELECT %s AS CLAIM_ID, %s AS ENCOUNTER_ID, %s AS CLAIM_TYPE, %s AS STATUS, %s AS PATIENT_NAME, "
            "         %s::NUMBER(14,2) AS CLAIM_AMOUNT, PARSE_JSON(%s) AS CLAIM_JSON, %s AS ACTOR) s "
            "ON t.CLAIM_ID = s.CLAIM_ID "
            "WHEN MATCHED THEN UPDATE SET ENCOUNTER_ID = s.ENCOUNTER_ID, CLAIM_TYPE = s.CLAIM_TYPE, STATUS = s.STATUS, "
            "  PATIENT_NAME = s.PATIENT_NAME, CLAIM_AMOUNT = s.CLAIM_AMOUNT, CLAIM_JSON = s.CLAIM_JSON, "
            "  IS_DELETED = FALSE, UPDATED_AT = CURRENT_TIMESTAMP() "
            "WHEN NOT MATCHED THEN INSERT (CLAIM_ID, ENCOUNTER_ID, CLAIM_TYPE, STATUS, PATIENT_NAME, CLAIM_AMOUNT, "
            "  CLAIM_JSON, CREATED_BY) VALUES (s.CLAIM_ID, s.ENCOUNTER_ID, s.CLAIM_TYPE, s.STATUS, s.PATIENT_NAME, "
            "  s.CLAIM_AMOUNT, s.CLAIM_JSON, s.ACTOR)",
            (str(claim["Claim ID"]), encounter_id, claim_type, str(claim.get("Status") or ""),
             str(info.get("Patient Name") or ""), _amount(claim), client.to_json(claim), actor or "system"),
        )


def save_validation_report(claim_id: str, report: dict[str, Any]) -> None:
    client.execute(
        "UPDATE BILLING.CLAIMS SET VALIDATION_REPORT = PARSE_JSON(%s), UPDATED_AT = CURRENT_TIMESTAMP() "
        "WHERE CLAIM_ID = %s",
        (client.to_json(report), claim_id),
    )


def load_claims() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Active claims (oldest first) and their saved validation reports keyed by Claim ID."""
    rows = client.query(
        "SELECT CLAIM_ID, CLAIM_JSON, VALIDATION_REPORT FROM BILLING.CLAIMS WHERE NOT IS_DELETED ORDER BY CREATED_AT"
    )
    claims = [client.variant(row["CLAIM_JSON"]) for row in rows]
    reports = {row["CLAIM_ID"]: client.variant(row["VALIDATION_REPORT"]) for row in rows if row["VALIDATION_REPORT"]}
    return [claim for claim in claims if isinstance(claim, dict)], reports


# ---------- PDF documents ----------
def archive_document(
    document_type: str,
    file_name: str,
    content: bytes,
    *,
    encounter_id: str = "",
    record_id: str = "",
    patient_name: str = "",
    reference_id: str = "",
    metadata: dict[str, Any] | None = None,
    actor: str = "",
    mime_type: str = "application/pdf",
) -> dict[str, Any]:
    """Store a rendered document; identical content for the same record is stored once."""
    digest = hashlib.sha256(content).hexdigest()
    existing = client.query(
        "SELECT DOCUMENT_ID, CREATED_AT FROM BILLING.BILL_DOCUMENTS "
        "WHERE SHA256 = %s AND DOCUMENT_TYPE = %s AND COALESCE(RECORD_ID, '') = %s LIMIT 1",
        (digest, document_type, record_id or ""),
    )
    if existing:
        return {"document_id": existing[0]["DOCUMENT_ID"], "created": False, "sha256": digest}
    document_id = str(uuid.uuid4())
    client.execute(
        "INSERT INTO BILLING.BILL_DOCUMENTS (DOCUMENT_ID, DOCUMENT_TYPE, ENCOUNTER_ID, RECORD_ID, PATIENT_NAME, "
        "  REFERENCE_ID, FILE_NAME, MIME_TYPE, SIZE_BYTES, SHA256, CONTENT, METADATA, CREATED_BY) "
        "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TO_BINARY(%s, 'BASE64'), PARSE_JSON(%s), %s",
        (document_id, document_type, encounter_id, record_id, patient_name, reference_id, file_name, mime_type,
         len(content), digest, _b64(content), client.to_json(metadata or {}), actor or "system"),
    )
    return {"document_id": document_id, "created": True, "sha256": digest}


def _b64(content: bytes) -> str:
    import base64

    return base64.b64encode(content).decode("ascii")


def list_documents(record_id: str | None = None, document_type: str | None = None) -> list[dict[str, Any]]:
    where, params = ["TRUE"], []
    if record_id:
        where.append("RECORD_ID = %s")
        params.append(record_id)
    if document_type:
        where.append("DOCUMENT_TYPE = %s")
        params.append(document_type)
    rows = client.query(
        "SELECT DOCUMENT_ID, DOCUMENT_TYPE, ENCOUNTER_ID, RECORD_ID, PATIENT_NAME, REFERENCE_ID, FILE_NAME, "
        "SIZE_BYTES, SHA256, CREATED_BY, CREATED_AT FROM BILLING.BILL_DOCUMENTS WHERE " + " AND ".join(where)
        + " ORDER BY CREATED_AT DESC LIMIT 200",
        tuple(params),
    )
    return [{key.lower(): value for key, value in row.items()} for row in rows]


def get_document(document_id: str) -> tuple[str, bytes] | None:
    rows = client.query(
        "SELECT FILE_NAME, BASE64_ENCODE(CONTENT) AS B64 FROM BILLING.BILL_DOCUMENTS WHERE DOCUMENT_ID = %s",
        (document_id,),
    )
    if not rows:
        return None
    import base64

    return rows[0]["FILE_NAME"], base64.b64decode(rows[0]["B64"])
