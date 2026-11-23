"""ICD-10-CM and CPT/HCPCS matching shared by the /codes endpoint and the coding workspace.

One implementation for both callers (the old UI called this over HTTP and kept a second, drifting copy
as an offline fallback).
"""

from __future__ import annotations

from cpt_coder import get_cpt_candidate_sets, get_cpt_codes, infer_documented_procedures
from icd10_coder import get_icd10_codes

MATCH_THRESHOLD = 80


def match_codes(conditions: list[str], procedures: list[str], documentation: str = "") -> dict[str, list[dict]]:
    """Return {"icd10": [...], "cpt": [...]} for explicitly supplied conditions and procedures."""
    conditions = [str(item).strip() for item in conditions or [] if str(item or "").strip()]
    documentation = str(documentation or "")
    icd10 = get_icd10_codes(conditions, threshold=MATCH_THRESHOLD) if conditions else []
    procedures = [str(item).strip() for item in procedures or [] if str(item or "").strip()]
    procedures += infer_documented_procedures(documentation)
    lowered = documentation.lower()
    if any(term in lowered for term in ("a1c", "hba1c", "hemoglobin a1c")):
        procedures.append("A1C Test")
    if ("chest x-ray" in lowered or "chest x ray" in lowered) and any(
        term in lowered for term in ("two view", "to view", "2 view")
    ):
        procedures.append("Chest x ray 2 views")
    procedures = list(dict.fromkeys(procedures))
    if not procedures:
        cpt = []
    elif documentation:
        cpt = [
            {
                "Extracted Procedure": item["term"],
                "Matched Procedure/Service": item["selected_description"],
                "CPT/HCPCS Code": item["selected_code"],
            }
            for item in get_cpt_candidate_sets(procedures, documentation, threshold=MATCH_THRESHOLD)
            if item["status"] == "suggested"
        ]
    else:
        cpt = get_cpt_codes(procedures, threshold=MATCH_THRESHOLD)
    return {"icd10": icd10, "cpt": cpt}
