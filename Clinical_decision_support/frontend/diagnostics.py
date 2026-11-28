"""Diagnostic orders: which tests the plan implies, why, whether they are due, and when they are billable.

Doctors rarely say "I'll order an HbA1c" out loud, so the ambient scribe never hears it and the test
never reaches the note, the EHR or the bill. This module closes that gap without an LLM: it reads the
assessment and plan, the CDS recommendations, the coded diagnoses and the chart (problem list and the
last result of each test) and proposes the tests, each with its reason and a due/not-due check.

Orders then follow a real lifecycle - Ordered -> Performed (specimen collected / study done) -> Resulted -
and only performed or resulted tests become billable lines. Clinicians see test names, never codes:
CPT/LOINC travel with the order for coding and billing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.rules import packs

ORDERED, PERFORMED, RESULTED, CANCELLED = "Ordered", "Performed", "Resulted", "Cancelled"
ACTIVE_STATUSES = (ORDERED, PERFORMED, RESULTED)
BILLABLE_STATUSES = (PERFORMED, RESULTED)
VENIPUNCTURE_CPT = "36415"
IN_HOUSE_LAB = "NuuCare In-house Laboratory"


@dataclass(frozen=True)
class DiagnosticTest:
    key: str
    name: str
    category: str  # Laboratory, Imaging, Cardiology, Pulmonary, Screening
    cpts: tuple[str, ...]
    loinc: str = ""
    specimen: str = ""  # Blood / Urine / Swab, empty for studies
    unit: str = ""
    low: float | None = None
    high: float | None = None
    reference: str = ""
    indications: tuple[str, ...] = ()  # ICD-10-CM prefixes that support medical necessity
    keywords: tuple[str, ...] = ()  # the test itself named in CDS / plan text
    conditions: tuple[str, ...] = ()  # conditions in the assessment that call for the test
    interval_days: int = 0  # minimum days between repeats (0 = no frequency limit)
    chronic_monitoring: bool = False  # may be proposed from the problem list alone
    point_of_care: bool = False  # result available during the visit
    prior_auth: bool = False  # payer authorization commonly required (commercial / Medicare Advantage)
    aliases: tuple[str, ...] = field(default=())

    @property
    def history_loincs(self) -> tuple[str, ...]:
        """The test's own LOINC plus component results that show the panel was drawn."""
        return (self.loinc, *_state()["panels"].get(self.key, ())) if self.loinc else ()

    @property
    def primary_cpt(self) -> str:
        return self.cpts[0]

    @property
    def performed_label(self) -> str:
        return "Specimen collected" if self.specimen else "Performed"


_NEGATION = re.compile(r"\b(no|not|denies|denied|without|negative for|ruled out|rules out|never)\b[^.;\n]{0,40}$")
_BUILT: dict[str, Any] = {"pack": None}


def _pattern(phrases: tuple[str, ...]) -> re.Pattern | None:
    if not phrases:
        return None
    alternatives = "|".join(re.escape(phrase) for phrase in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])")


def _state() -> dict[str, Any]:
    """The test catalog from the diagnostic_catalog rule pack, rebuilt only when the pack changes."""
    pack = packs.load_pack("diagnostic_catalog")
    if _BUILT["pack"] is not pack:
        tests = tuple(
            DiagnosticTest(**{field: tuple(value) if isinstance(value, list) else value for field, value in item.items()})
            for item in pack["tests"]
        )
        _BUILT.update(
            pack=pack,
            catalog=tests,
            by_key={test.key: test for test in tests},
            keyword={test.key: _pattern(test.keywords) for test in tests},
            condition={test.key: _pattern(test.conditions) for test in tests},
            panels={key: tuple(value) for key, value in (pack.get("panel_components") or {}).items()},
            nonspecific=set(pack.get("non_specific_findings") or ()),
        )
    return _BUILT


def catalog() -> tuple[DiagnosticTest, ...]:
    return _state()["catalog"]


def by_key() -> dict[str, DiagnosticTest]:
    return _state()["by_key"]


def __getattr__(name: str) -> Any:
    """Module attributes CATALOG / BY_KEY / ... always reflect the current pack."""
    mapping = {"CATALOG": "catalog", "BY_KEY": "by_key", "PANEL_COMPONENTS": "panels", "NON_SPECIFIC_FINDINGS": "nonspecific"}
    if name in mapping:
        return _state()[mapping[name]]
    raise AttributeError(name)


def _mention(pattern: re.Pattern | None, text: str) -> str | None:
    """First phrase the pattern finds that is not negated in the same clause ("denies chest pain")."""
    if pattern is None or not text:
        return None
    for match in pattern.finditer(text):
        if not _NEGATION.search(text[max(0, match.start() - 60):match.start()]):
            return match.group(0)
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d %b %Y %H:%M", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _rows(chart: dict[str, Any] | None, sheet: str) -> list[dict[str, Any]]:
    rows = ((chart or {}).get("records") or {}).get(sheet) or []
    return [row for row in rows if isinstance(row, dict)]


def _section(note: str, *headings: str) -> str:
    """Text under the given SOAP headings (Assessment, Plan), or the whole note if it has no headings."""
    text = str(note or "")
    names = "|".join(re.escape(heading) for heading in headings)
    match = re.search(rf"(?ims)^\s*#*\s*(?:{names})\b[^\n]*\n(.*?)(?=^\s*#*\s*(?:subjective|objective|assessment|plan)\b|\Z)", text)
    return match.group(1) if match else text


def encounter_diagnoses(record: dict[str, Any], chart: dict[str, Any] | None) -> list[dict[str, str]]:
    """Coded encounter diagnoses first, then the active problem list; one row per ICD-10-CM code."""
    seen, rows = set(), []
    for item in ((record or {}).get("code_results") or {}).get("icd10", []) or []:
        code = str((item or {}).get("ICD-10 Code") or "").strip().upper()
        if code and code not in seen:
            seen.add(code)
            rows.append({"code": code, "description": str(item.get("Matched Disease/Injury") or ""), "source": "Encounter"})
    for problem in _rows(chart, "Problems"):
        code = str(problem.get("ICD_10_CM") or "").strip().upper()
        status = str(problem.get("Clinical_Status") or "Active").strip().lower()
        if code and code not in seen and status in {"active", "recurrence", "relapse", ""}:
            seen.add(code)
            rows.append({"code": code, "description": str(problem.get("Problem_Name") or ""), "source": "Problem list"})
    return rows


def last_results(chart: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Most recent result per LOINC code from the chart's Labs sheet."""
    latest: dict[str, dict[str, Any]] = {}
    for row in _rows(chart, "Labs"):
        loinc = str(row.get("LOINC_Code") or "").strip()
        when = _parse_date(row.get("Result_Date"))
        if not loinc or when is None:
            continue
        if loinc not in latest or when > latest[loinc]["date"]:
            latest[loinc] = {"date": when, "result": row.get("Result"), "unit": row.get("Unit") or "", "flag": row.get("Flag") or ""}
    return latest


def due_status(test: DiagnosticTest, latest: dict[str, dict[str, Any]], today: date) -> dict[str, Any]:
    candidates = [latest[code] for code in test.history_loincs if code in latest]
    previous = max(candidates, key=lambda item: item["date"]) if candidates else None
    if previous is None:
        return {"due": True, "label": "Due · no prior result on file", "last": None, "next_due": None}
    last_text = f"{previous['result']} {previous['unit']}".strip()
    if test.interval_days:
        next_due = previous["date"] + timedelta(days=test.interval_days)
        if next_due > today:
            return {
                "due": False,
                "label": f"Not due · last {last_text} on {previous['date']:%d %b %Y}; repeat from {next_due:%d %b %Y}",
                "last": previous,
                "next_due": next_due,
            }
    return {"due": True, "label": f"Due · last {last_text} on {previous['date']:%d %b %Y}", "last": previous, "next_due": None}


def linked_diagnoses(test: DiagnosticTest, diagnoses: list[dict[str, str]]) -> list[str]:
    return [row["code"] for row in diagnoses if any(row["code"].startswith(prefix) for prefix in test.indications)]


def active_orders(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [order for order in (record or {}).get("diagnostic_orders") or [] if order.get("status") in ACTIVE_STATUSES]


def suggest_tests(record: dict[str, Any], chart: dict[str, Any] | None, today: date | None = None) -> list[dict[str, Any]]:
    """Tests the encounter implies, strongest evidence first.

    Evidence: the CDS recommendation names the test (3), the assessment/plan names the test or a
    condition that calls for it (2), a coded encounter diagnosis supports it (2), the active problem
    list supports a chronic-monitoring test (1). Tests already ordered on this encounter are skipped.
    """
    today = today or date.today()
    note = str((record or {}).get("summary") or "")
    plan_text = (_section(note, "assessment", "plan") + "\n" + str((record or {}).get("patient_summary") or "")).lower()
    cds_text = str((record or {}).get("recommendations") or "").lower()
    diagnoses = encounter_diagnoses(record, chart)
    latest = last_results(chart)
    already = {order.get("test_key") for order in active_orders(record)}
    suggestions = []
    state = _state()
    for test in state["catalog"]:
        if test.key in already:
            continue
        reasons, strength = [], 0
        if phrase := _mention(state["keyword"][test.key], cds_text):
            reasons.append(f"CDS recommendation mentions “{phrase}”")
            strength = max(strength, 3)
        if phrase := _mention(state["keyword"][test.key], plan_text):
            reasons.append(f"Plan mentions “{phrase}”")
            strength = max(strength, 2)
        elif phrase := _mention(state["condition"][test.key], plan_text):
            reasons.append(f"Assessment: {phrase}")
            # A non-specific symptom (fatigue, fever...) is shown as an option but not pre-selected.
            strength = max(strength, 1 if phrase in state["nonspecific"] else 2)
        linked = linked_diagnoses(test, diagnoses)
        encounter_dx = [row for row in diagnoses if row["code"] in linked and row["source"] == "Encounter"]
        problem_dx = [row for row in diagnoses if row["code"] in linked and row["source"] == "Problem list"]
        if encounter_dx:
            reasons.append("Diagnosis " + ", ".join(f"{row['description'] or row['code']}" for row in encounter_dx[:2]))
            # A diagnosis alone pre-selects only standard-of-care monitoring (e.g. A1c for diabetes).
            strength = max(strength, 2 if test.chronic_monitoring else 1)
        if problem_dx and test.chronic_monitoring:
            reasons.append("Problem list: " + ", ".join(row["description"] or row["code"] for row in problem_dx[:2]))
            strength = max(strength, 1)
        if not strength:
            continue
        due = due_status(test, latest, today)
        condition = _mention(state["condition"][test.key], plan_text)
        if encounter_dx:
            note_reason = f"for {(encounter_dx[0]['description'] or encounter_dx[0]['code']).lower()}"
        elif condition:
            note_reason = f"for {condition}"
        elif any(reason.startswith("CDS") for reason in reasons):
            note_reason = "recommended by clinical decision support"
        elif problem_dx:
            note_reason = f"monitoring {(problem_dx[0]['description'] or problem_dx[0]['code']).lower()}"
        else:
            note_reason = ""
        suggestions.append({
            "test_key": test.key,
            "name": test.name,
            "category": test.category,
            "reasons": reasons,
            "strength": strength,
            "due": due,
            "linked_dx": linked,
            "preselect": strength >= 2 and due["due"],
            "prior_auth_candidate": test.prior_auth,
            "note_reason": note_reason,
        })
    suggestions.sort(key=lambda item: (-item["strength"], not item["due"]["due"], item["name"]))
    return suggestions


def new_order(
    test_key: str,
    record: dict[str, Any],
    *,
    reasons: list[str] | None = None,
    linked_dx: list[str] | None = None,
    ordered_by: str = "",
    priority: str = "Routine",
    source: str = "CDS",
    now: datetime | None = None,
) -> dict[str, Any]:
    test = by_key()[test_key]
    now = now or datetime.now()
    return {
        "order_id": "",  # assigned by the EHR (ORD########) when the order is saved
        "test_key": test.key,
        "name": test.name,
        "category": test.category,
        "cpts": list(test.cpts),
        "loinc": test.loinc,
        "specimen": test.specimen,
        "status": ORDERED,
        "priority": priority if priority in {"Routine", "STAT"} else "Routine",
        "source": source,
        "reasons": list(reasons or []),
        "linked_dx": list(linked_dx or []),
        "ordered_by": ordered_by,
        "ordered_at": now.strftime("%Y-%m-%d %H:%M"),
        "encounter_id": str((record or {}).get("ehr_encounter_id") or (record or {}).get("id") or ""),
        "prior_auth": {"required": False},
        "result": None,
        "performed_at": None,
    }


def result_flag(test: DiagnosticTest, value: Any) -> str:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        text = str(value or "").strip().lower()
        if not text:
            return ""
        normal_words = ("negative", "normal", "nonreactive", "non-reactive", "unremarkable", "no acute", "within normal")
        return "Normal" if any(word in text for word in normal_words) else "Abnormal"
    if test.high is not None and number > test.high:
        return "High"
    if test.low is not None and number < test.low:
        return "Low"
    return "Normal"


def mark_performed(order: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    if order.get("status") == ORDERED:
        order["status"] = PERFORMED
        order["performed_at"] = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    return order


def record_result(order: dict[str, Any], value: Any, now: datetime | None = None) -> dict[str, Any]:
    """Store a result; resulting a test also means it was performed (the billable event)."""
    test = by_key()[order["test_key"]]
    text = str(value or "").strip()
    if not text:
        raise ValueError("Enter a result value.")
    now = now or datetime.now()
    if order.get("status") == CANCELLED:
        raise ValueError("A cancelled order cannot be resulted.")
    order["performed_at"] = order.get("performed_at") or now.strftime("%Y-%m-%d %H:%M")
    order["status"] = RESULTED
    order["result"] = {
        "value": text,
        "unit": test.unit,
        "reference": test.reference,
        "flag": result_flag(test, text),
        "resulted_at": now.strftime("%Y-%m-%d %H:%M"),
    }
    return order


def done_in_clinic(test: DiagnosticTest) -> bool:
    """Point-of-care tests and in-house specimen collection happen during the visit; studies are scheduled."""
    return bool(test.point_of_care or test.specimen)


def cancel_order(order: dict[str, Any], reason: str = "") -> dict[str, Any]:
    if order.get("status") in BILLABLE_STATUSES:
        raise ValueError("A performed test cannot be cancelled; correct it with a coder instead.")
    order["status"] = CANCELLED
    order["cancel_reason"] = reason
    return order


def plan_line(order: dict[str, Any], suggestion: dict[str, Any] | None = None) -> str:
    """The sentence added to the Plan: test name and the clinical reason, never a billing code."""
    reason = str((suggestion or {}).get("note_reason") or "")
    due = (suggestion or {}).get("due") or {}
    last = due.get("last")
    history = f" (last {last['result']}{'' if str(last['unit']).startswith('%') else ' '}{last['unit']} on {last['date']:%d %b %Y})" if last else ""
    priority = " (STAT)" if order.get("priority") == "STAT" else ""
    return f"Order: {order['name']}{priority}" + (f" — {reason}" if reason else "") + history


def ehr_order_row(order: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Orders_Procedures row: the EHR (Snowflake EHR.SHEETS) copy of the order, linked by MRN and encounter."""
    test = by_key()[order["test_key"]]
    pa = order.get("prior_auth") or {}
    result = order.get("result") or {}
    return {
        "Order_ID": order.get("order_id") or "",
        "Patient_ID": str((record or {}).get("ehr_patient_id") or ""),
        "MRN": str((record or {}).get("mrn") or ""),
        "Encounter_ID": order.get("encounter_id") or str((record or {}).get("ehr_encounter_id") or ""),
        "Order_Date": str(order.get("ordered_at") or "")[:10],
        "Order_Type": test.category,
        "CPT_HCPCS": "+".join(test.cpts),
        "LOINC_Code": test.loinc,
        "Order_Description": test.name,
        "Status": order.get("status"),
        "Ordering_Provider": (record or {}).get("doctor") or order.get("ordered_by") or "",
        "Facility": "NuuCare Demo",
        "Priority": order.get("priority") or "Routine",
        "Linked_Diagnosis": ";".join(order.get("linked_dx") or []),
        "Order_Source": order.get("source") or "",
        "Prior_Auth_Status": pa.get("status") or ("Not required" if not pa.get("required") else "Required"),
        "Auth_Number": pa.get("auth_number") or "",
        "Performed_Date": str(order.get("performed_at") or "")[:10],
        "Result_Value": result.get("value") or "",
        "Result_Flag": result.get("flag") or "",
    }


def ehr_lab_row(order: dict[str, Any], record: dict[str, Any]) -> dict[str, Any] | None:
    """Labs row for a resulted laboratory test (so the next visit's due-check sees it)."""
    test = by_key()[order["test_key"]]
    result = order.get("result") or {}
    if order.get("status") != RESULTED or not test.loinc:
        return None
    return {
        "Lab_ID": order.get("lab_id") or "",
        "Patient_ID": str((record or {}).get("ehr_patient_id") or ""),
        "Result_Date": str(result.get("resulted_at") or "")[:10],
        "LOINC_Code": test.loinc,
        "Test_Name": test.name,
        "Result": result.get("value"),
        "Unit": test.unit,
        "Reference_Range": test.reference,
        "Flag": result.get("flag") or "",
        "Status": "Final",
        "Performing_Lab": IN_HOUSE_LAB,
        "Ordering_Provider": (record or {}).get("doctor") or "",
        "Encounter_ID": order.get("encounter_id") or "",
        "Order_ID": order.get("order_id") or "",
    }


def billing_lines(record: dict[str, Any]) -> list[dict[str, Any]]:
    """One line per CPT of every active order, with its billing state.

    Performed/resulted tests are billable (the order and its result are the performed evidence).
    Tests still only ordered are held, and a test whose payer authorization is not approved is held
    so the claim is not denied for missing authorization (CARC 197). Blood drawn in-house adds one
    venipuncture (36415) per encounter.
    """
    lines = []
    blood_dx: list[str] = []
    blood_drawn = False
    encounter_dx = encounter_diagnoses(record, None)
    for order in active_orders(record):
        test = by_key().get(order.get("test_key"))
        if test is None:
            continue
        # Diagnoses coded after the order was placed still justify it (medical necessity / pointers).
        linked = list(dict.fromkeys([*(order.get("linked_dx") or []), *linked_diagnoses(test, encounter_dx)]))
        pa = order.get("prior_auth") or {}
        status = order.get("status")
        result = order.get("result") or {}
        if status in BILLABLE_STATUSES and pa.get("required") and pa.get("status") != "Approved":
            state, reason = "HELD", f"Prior authorization {str(pa.get('status') or 'missing').lower()} — CARC 197 denial risk"
        elif status in BILLABLE_STATUSES:
            state = "BILLABLE"
            reason = f"{test.performed_label} {str(order.get('performed_at') or '')[:10]}".strip()
            if result:
                reason += f" · result {result.get('value')} {result.get('unit') or ''}".rstrip() + (
                    f" ({result['flag']})" if result.get("flag") and result["flag"] != "Normal" else "")
        else:
            state, reason = "PENDING", "Ordered — not yet performed"
        for cpt in test.cpts:
            lines.append({
                "code": cpt,
                "service": test.name,
                "order_id": order.get("order_id") or "",
                "test_key": test.key,
                "state": state,
                "reason": reason,
                "linked_dx": linked,
                "auth_number": pa.get("auth_number") or "",
                "prior_auth_required": bool(pa.get("required")),
            })
        if state == "BILLABLE" and test.specimen == "Blood":
            blood_drawn = True
            blood_dx.extend(linked)
    if blood_drawn:
        lines.append({
            "code": VENIPUNCTURE_CPT,
            "service": "Routine venipuncture (specimen collection)",
            "order_id": "",
            "test_key": "",
            "state": "BILLABLE",
            "reason": "Blood specimen collected in-house",
            "linked_dx": list(dict.fromkeys(blood_dx)),
            "auth_number": "",
            "prior_auth_required": False,
        })
    return lines
