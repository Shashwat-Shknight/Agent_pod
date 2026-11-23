"""Official code-set API: status, validation, search and on-demand sync (ICD-10-CM, HCPCS)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.db.settings import use_snowflake

router = APIRouter(prefix="/reference", tags=["code sets"])


def _require_snowflake() -> None:
    if not use_snowflake():
        raise HTTPException(status_code=409, detail="Official code sets need STORAGE_BACKEND=snowflake")


@router.get("/status")
def code_set_status() -> dict[str, Any]:
    """Active release per code system, codes in force and the latest sync runs."""
    _require_snowflake()
    from app.db import client
    from app.reference import sync

    runs = client.query(
        "SELECT CODE_SYSTEM, TRIGGERED_BY, STATUS, RELEASE_ID, MESSAGE, STARTED_AT, FINISHED_AT "
        "FROM REFERENCE.SYNC_RUNS ORDER BY STARTED_AT DESC LIMIT 10"
    )
    return {
        "code_sets": [{key.lower(): value for key, value in row.items()} for row in sync.status()],
        "cpt": "Not loaded: CPT is licensed by the AMA. CPT codes pass through as 'unverified'.",
        "recent_syncs": [{key.lower(): value for key, value in row.items()} for row in runs],
    }


@router.get("/validate")
def validate(
    codes: str = Query(..., description="Comma-separated codes, e.g. R51,I10,J0013"),
    date_of_service: str | None = Query(None, description="YYYY-MM-DD (default: today)"),
    kind: str | None = Query(None, description="ICD for diagnoses, CPT for procedure/HCPCS codes"),
    live: bool = Query(False, description="Also confirm against the live NLM API"),
) -> dict[str, Any]:
    _require_snowflake()
    from app.reference import lookup

    items = [code.strip() for code in codes.split(",") if code.strip()]
    if not items:
        raise HTTPException(status_code=422, detail="No codes supplied")
    check = lookup.validate_for_claim if live else lookup.validate_codes
    results = check(items, date_of_service, kind)
    return {**lookup.summarize(results), "results": results}


@router.get("/search")
def search(
    q: str = Query(..., min_length=2),
    system: str = Query("ICD10CM", pattern="^(ICD10CM|HCPCS)$"),
    limit: int = Query(20, ge=1, le=100),
    billable_only: bool = True,
) -> dict[str, Any]:
    _require_snowflake()
    from app.reference import lookup

    return {"system": system, "results": lookup.search(system, q, limit=limit, billable_only=billable_only)}


@router.get("/changes")
def changes(
    system: str = Query("ICD10CM", pattern="^(ICD10CM|HCPCS)$"),
    release: str | None = None,
    limit: int = Query(200, ge=1, le=5000),
) -> dict[str, Any]:
    """Codes added, revised or deleted by a release (default: every release after the baseline)."""
    _require_snowflake()
    from app.db import client

    rows = client.query(
        "SELECT RELEASE_ID, CHANGE, CODE, LONG_DESCRIPTION, CHANGE_DATE FROM REFERENCE.V_CODE_CHANGES "
        "WHERE CODE_SYSTEM = %s AND (%s IS NULL OR RELEASE_ID = %s) ORDER BY CHANGE_DATE DESC, CODE LIMIT %s",
        (system, release, release, limit),
    )
    return {"system": system, "changes": [{key.lower(): value for key, value in row.items()} for row in rows]}


@router.post("/sync")
def run_sync(
    system: str = Query("all", pattern="^(all|ICD10CM|HCPCS)$"),
    force: bool = Query(False, description="Refresh HCPCS even if the snapshot is recent"),
) -> dict[str, Any]:
    """Pull the latest official releases now (normally done automatically every 24 h)."""
    _require_snowflake()
    from app.reference import sync

    systems = sync.SYSTEMS if system == "all" else (system,)
    results = sync.sync_all("api", systems, force=force)
    if any(r["system"] == "ICD10CM" and r["status"] == "SUCCEEDED" for r in results):
        import code_matcher

        code_matcher.load_icd10_data(force_reload=True)
    return {"results": results}
