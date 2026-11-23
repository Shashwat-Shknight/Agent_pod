from io import BytesIO

from pypdf import PdfReader

from app.domain.pdf_documents import bill_pdf_bytes, claim_pdf_bytes

EXPORT = {
    "encounter_id": "ENC-1001",
    "patient": "Jane <Demo> & Co",
    "encounter_type": "OPD",
    "lines": [
        {"CPT/HCPCS Code": "71046", "Service Description": "Chest X-ray, 2 views", "Department": "Radiology",
         "Quantity": 1, "Unit Charge": "$120.00", "Gross Charge": "$120.00", "Pricing Status": "Priced"},
        {"CPT/HCPCS Code": "99214", "Service Description": "Office visit, moderate MDM", "Department": "Clinic",
         "Quantity": 1, "Unit Charge": "$180.00", "Gross Charge": "$180.00", "Pricing Status": "Priced"},
    ],
    "gross_total": 300.0,
    "held_lines": [{"code": "70450", "reason": "Not documented as performed"}],
    "notice": "Demo charges only.",
}

CLAIM = {
    "Claim ID": "CLM-0001", "Encounter ID": "ENC-1001", "Claim Type": "837P", "Status": "Draft",
    "Patient Information": {"Patient Name": "Jane Demo", "Patient ID": "P-1"},
    "Rendering Provider": {"Name": "Dr Demo", "NPI": "DEMO-NPI"}, "Place of Service": "11",
    "ICD-10 Codes": ["R05.9"], "CPT/HCPCS Codes": ["71046"],
    "Charges": [{"CPT/HCPCS Codes": ["71046"], "Service Description": "Chest X-ray", "Quantity": 1,
                 "Charge Amount ($)": 120.0, "Diagnosis Pointers": ["A"]}],
    "Claim Amount": 120.0,
    "Supporting Documents Validation": {"Overall Status": "Complete", "Results": [
        {"CPT Code": "71046", "Procedure": "Chest X-ray", "Required Document": "X-Ray Report", "Status": "Present"}]},
    "Clinical Context": {"SOAP Note": "Subjective: cough\nPlan: chest X-ray"},
}


def _text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)


def test_bill_pdf_contains_lines_totals_and_escapes_markup():
    pdf = bill_pdf_bytes(EXPORT, {"id": "PT-1", "doctor": "Dr Demo", "status": "Approved"})
    assert pdf.startswith(b"%PDF")
    text = _text(pdf)
    for expected in ("Draft Hospital Bill", "ENC-1001", "71046", "99214", "$300.00", "70450", "Jane <Demo> & Co"):
        assert expected in text


def test_bill_pdf_is_reproducible_for_deduplicated_archiving():
    assert bill_pdf_bytes(EXPORT) == bill_pdf_bytes(EXPORT)


def test_claim_pdf_renders_professional_claim():
    text = _text(claim_pdf_bytes(CLAIM))
    for expected in ("837P Claim Draft", "CLM-0001", "R05.9", "$120.00", "X-Ray Report", "not a payer-ready"):
        assert expected in text


def test_claim_pdf_tolerates_missing_sections():
    pdf = claim_pdf_bytes({"Claim ID": "CLM-2", "Claim Type": "837I"})
    assert "No charges" in _text(pdf)
