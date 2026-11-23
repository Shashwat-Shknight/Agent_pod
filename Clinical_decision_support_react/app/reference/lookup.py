"""Code-set lookups against REFERENCE.CODES.

``validate_codes`` answers, for each code: is it a real code, was it in force on the date of
service, and is it billable (ICD-10-CM leaf code)? Invalid codes come with replacement
suggestions (e.g. R51 -> R51.0 / R51.9). CPT codes are reported as ``unverified`` because the
CPT code set is AMA-licensed and not loaded.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from typing import Any, Iterable

from app.db import client

ICD10CM, HCPCS, CPT = "ICD10CM", "HCPCS", "CPT"
_ICD_PATTERN = re.compile(r"^[A-Z][0-9][0-9A-Z]{1,5}$")
_HCPCS_PATTERN = re.compile(r"^[A-V][0-9]{4}$")
_CPT_PATTERN = re.compile(r"^[0-9]{4}[0-9FTU]$")
CACHE_SECONDS = 600

_CACHE: dict[tuple, tuple[float, Any]] = {}
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _cached(key: tuple, loader):
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = loader()
    with _LOCK:
        _CACHE[key] = (now, value)
    return value


def raw(code: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(code or "").upper())


def classify(code: str, hint: str | None = None) -> str:
    """Which code system a code belongs to: ICD10CM, HCPCS, CPT or '' (unknown)."""
    value, hint = raw(code), (hint or "").upper()
    # ICD codes without their dot (A0101 = A01.01) look like HCPCS, so context decides.
    if hint.startswith("ICD") or "." in str(code):
        return ICD10CM if _ICD_PATTERN.match(value) else ""
    if hint.startswith(("CPT", "HCPCS", "PROC")):
        return HCPCS if _HCPCS_PATTERN.match(value) else CPT if _CPT_PATTERN.match(value) else ""
    if _HCPCS_PATTERN.match(value):
        return HCPCS
    if _CPT_PATTERN.match(value):
        return CPT
    if _ICD_PATTERN.match(value):
        return ICD10CM
    return ""


def as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date()
        except ValueError:
            continue
    return date.today()


def _versions(system: str, codes: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    if not codes:
        return {}
    placeholders = ", ".join(["%s"] * len(codes))
    rows = client.query(
        "SELECT CODE_RAW, CODE, LONG_DESCRIPTION, IS_BILLABLE, VALID_FROM, VALID_TO, RELEASE_ID, RETIRED_IN_RELEASE "
        f"FROM REFERENCE.CODES WHERE CODE_SYSTEM = %s AND CODE_RAW IN ({placeholders}) ORDER BY VALID_FROM",
        (system, *codes),
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["CODE_RAW"], []).append(row)
    return grouped


def _children(system: str, code_raw: str, on: date, limit: int = 6) -> list[dict[str, str]]:
    rows = client.query(
        "SELECT CODE, LONG_DESCRIPTION FROM REFERENCE.CODES WHERE CODE_SYSTEM = %s AND IS_BILLABLE "
        "AND STARTSWITH(CODE_RAW, %s) AND CODE_RAW <> %s AND VALID_FROM <= %s AND (VALID_TO IS NULL OR VALID_TO >= %s) "
        "ORDER BY CODE_RAW LIMIT %s",
        (system, code_raw, code_raw, on, on, limit),
    )
    return [{"code": row["CODE"], "description": row["LONG_DESCRIPTION"]} for row in rows]


def code_sets_loaded() -> dict[str, str]:
    """{system: active release id} - empty when nothing has been synced yet."""
    def load():
        rows = client.query("SELECT CODE_SYSTEM, ACTIVE_RELEASE FROM REFERENCE.V_CODE_SET_STATUS")
        return {row["CODE_SYSTEM"]: row["ACTIVE_RELEASE"] for row in rows}

    return _cached(("status",), load)


def validate_codes(codes: Iterable[str], date_of_service: Any = None, hint: str | None = None) -> list[dict[str, Any]]:
    """Validate codes as of the date of service. Returns one result dict per input code.

    status: valid | not_billable | not_yet_effective | deleted | not_found | unverified | unknown_format
    """
    on = as_date(date_of_service)
    items = [(str(code).strip(), classify(code, hint)) for code in codes if str(code or "").strip()]
    loaded = code_sets_loaded()
    results = []
    for system in (ICD10CM, HCPCS):
        wanted = tuple(sorted({raw(code) for code, kind in items if kind == system}))
        if wanted and system in loaded:
            versions = _cached(("versions", system, wanted), lambda s=system, w=wanted: _versions(s, w))
        else:
            versions = {}
        for code, kind in items:
            if kind != system:
                continue
            results.append(_evaluate(code, system, versions.get(raw(code), []), on, system in loaded))
    for code, kind in items:
        if kind == CPT:
            results.append({"code": code, "system": CPT, "status": "unverified", "ok": True,
                            "message": "CPT is AMA-licensed; not verified against an official CPT release"})
        elif not kind:
            results.append({"code": code, "system": "", "status": "unknown_format", "ok": False,
                            "message": "Not a recognisable ICD-10-CM, HCPCS or CPT code"})
    order = {code: index for index, (code, _kind) in enumerate(items)}
    return sorted(results, key=lambda result: order.get(result["code"], 0))


def _evaluate(code: str, system: str, versions: list[dict[str, Any]], on: date, loaded: bool) -> dict[str, Any]:
    label = "ICD-10-CM" if system == ICD10CM else "HCPCS"
    base = {"code": code, "system": system, "date_of_service": on.isoformat()}
    if not loaded:
        return {**base, "status": "unverified", "ok": True, "message": f"{label} code set not synced yet"}
    if not versions:
        return {**base, "status": "not_found", "ok": False, "message": f"Not a valid {label} code"}
    current = next((v for v in versions if v["VALID_FROM"] <= on and (v["VALID_TO"] is None or v["VALID_TO"] >= on)), None)
    latest = versions[-1]
    if current is None:
        if versions[0]["VALID_FROM"] > on:
            return {**base, "status": "not_yet_effective", "ok": False,
                    "message": f"Effective from {versions[0]['VALID_FROM']} ({versions[0]['RELEASE_ID']})"}
        replacements = _children(system, raw(code), on)
        return {**base, "status": "deleted", "ok": False, "replacements": replacements,
                "message": f"Deleted after {latest['VALID_TO']} ({latest['RETIRED_IN_RELEASE'] or 'retired'})",
                "description": latest["LONG_DESCRIPTION"]}
    result = {**base, "description": current["LONG_DESCRIPTION"], "release": current["RELEASE_ID"]}
    if not current["IS_BILLABLE"]:
        return {**result, "status": "not_billable", "ok": False, "replacements": _children(system, raw(code), on),
                "message": "Category header, not billable - a more specific code is required"}
    return {**result, "status": "valid", "ok": True, "message": f"Valid and billable on {on.isoformat()}"}


def search(system: str, text: str, limit: int = 20, billable_only: bool = True) -> list[dict[str, str]]:
    words = [word for word in re.findall(r"[A-Za-z0-9]+", text or "") if len(word) > 1][:6]
    if not words:
        return []
    code_hit = raw(text)
    where = " AND ".join(["LONG_DESCRIPTION ILIKE %s"] * len(words))
    rows = client.query(
        "SELECT CODE, LONG_DESCRIPTION, IS_BILLABLE FROM REFERENCE.V_CURRENT_CODES WHERE CODE_SYSTEM = %s "
        f"AND ((CODE_RAW = %s) OR ({where})) " + ("AND IS_BILLABLE " if billable_only else "")
        + "ORDER BY IFF(CODE_RAW = %s, 0, 1), LENGTH(LONG_DESCRIPTION) LIMIT %s",
        (system.upper(), code_hit, *[f"%{word}%" for word in words], code_hit, limit),
    )
    return [{"code": row["CODE"], "description": row["LONG_DESCRIPTION"], "billable": row["IS_BILLABLE"]} for row in rows]


def default_billable_child(header_raw: str, billable: list[tuple[str, str, str]]) -> tuple[str, str] | None:
    """Pick the code a coder defaults to when only the category is documented.

    ``billable`` is (code_raw, code, description) for the header's billable descendants. Preference:
    'unspecified' wording, no 'with <complication>', a ...9/...0 terminal digit, then the shortest code.
    """
    if not billable:
        return None

    def rank(item):
        code_raw, _code, description = item
        text = description.lower()
        return (
            "unspecified" not in text,
            " with " in f" {text} " and "without" not in text,
            code_raw[-1] not in "90",
            len(code_raw),
            code_raw,
        )

    best = min(billable, key=rank)
    return best[1], best[2]


def current_icd10cm_codes() -> list[dict[str, str]]:
    """The coding engine's diagnosis dictionary: every billable ICD-10-CM code in force today, plus each
    category header's wording mapped to its default billable code (e.g. "Headache" -> R51.9), so short
    documented terms still resolve to a code that can actually be billed."""
    from bisect import bisect_left

    rows = client.query(
        "SELECT CODE_RAW, CODE, LONG_DESCRIPTION, IS_BILLABLE FROM REFERENCE.V_CURRENT_CODES "
        "WHERE CODE_SYSTEM = 'ICD10CM' ORDER BY CODE_RAW"
    )
    billable = [(row["CODE_RAW"], row["CODE"], row["LONG_DESCRIPTION"]) for row in rows if row["IS_BILLABLE"]]
    keys = [item[0] for item in billable]
    dictionary = [{"icd10_code": code, "description": text, "display_description": text} for _raw, code, text in billable]
    for row in rows:
        if row["IS_BILLABLE"]:
            continue
        start = bisect_left(keys, row["CODE_RAW"])
        end = start
        while end < len(keys) and keys[end].startswith(row["CODE_RAW"]):
            end += 1
        chosen = default_billable_child(row["CODE_RAW"], billable[start:end])
        if chosen:
            dictionary.append({"icd10_code": chosen[0], "description": row["LONG_DESCRIPTION"], "display_description": chosen[1]})
    return dictionary


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    problems = [result for result in results if not result["ok"]]
    return {
        "ok": not problems,
        "checked": len(results),
        "problems": problems,
        "unverified_cpt": [result["code"] for result in results if result["system"] == CPT],
    }


def validate_for_claim(codes: Iterable[str], date_of_service: Any = None, hint: str | None = None) -> list[dict[str, Any]]:
    """validate_codes plus a live confirmation against the NLM Clinical Tables API for codes that
    pass locally (claims are the last gate before money moves). ``live_confirmed`` is True/False,
    or None when NLM could not be reached (the Snowflake result still stands)."""
    from concurrent.futures import ThreadPoolExecutor

    from app.reference.sources import live_lookup

    results = validate_codes(codes, date_of_service, hint)
    to_confirm = [result for result in results if result["ok"] and result["system"] in (ICD10CM, HCPCS)
                  and result["status"] == "valid"]
    if to_confirm:
        with ThreadPoolExecutor(max_workers=6) as pool:
            confirmations = list(pool.map(lambda result: live_lookup(result["system"], result["code"]), to_confirm))
        for result, confirmed in zip(to_confirm, confirmations):
            result["live_confirmed"] = confirmed
            if confirmed is False and as_date(date_of_service) >= date.today():
                result.update(ok=False, status="not_found_live",
                              message="Not found in the live NLM code set - re-sync code sets before billing")
    return results
