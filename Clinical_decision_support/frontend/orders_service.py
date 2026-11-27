"""Saving diagnostic orders, results and prior authorizations to the EHR (Snowflake EHR.SHEETS).

Every write goes through the FastAPI backend, which assigns hospital-style IDs (ORD########,
LAB########, PA########) under its store lock, so the orders, results and authorizations of one
encounter stay linked by Patient_ID, MRN, Encounter_ID and Order_ID.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from frontend import diagnostics as dx
from frontend import prior_auth as pa
from frontend.api_client import HealthcareApiClient
from frontend.clinical_text import append_to_plan
from frontend.ehr_chart import primary_coverage


def service_date(record: dict[str, Any]) -> date:
    try:
        return datetime.strptime(str(record.get("date") or ""), "%d/%m/%Y").date()
    except ValueError:
        return date.today()


def _patient_id(record: dict[str, Any]) -> str:
    patient_id = str(record.get("ehr_patient_id") or record.get("patient_id") or "")
    if not patient_id:
        raise ValueError("Link this consultation to an EHR patient before placing orders.")
    return patient_id


def sync_order(record: dict[str, Any], order: dict[str, Any], client: HealthcareApiClient | None = None) -> dict[str, Any]:
    """Upsert the order (and its lab result, once resulted) into the EHR; returns the order with its IDs."""
    client = client or HealthcareApiClient()
    patient_id = _patient_id(record)
    saved = client.upsert_patient_records(patient_id, "Orders_Procedures", [dx.ehr_order_row(order, record)], "Order_ID")
    if saved:
        order["order_id"] = saved[0].get("Order_ID") or order.get("order_id")
    lab = dx.ehr_lab_row(order, record)
    if lab is not None:
        lab["Order_ID"] = order["order_id"]
        rows = client.upsert_patient_records(patient_id, "Labs", [lab], "Lab_ID")
        if rows:
            order["lab_id"] = rows[0].get("Lab_ID") or order.get("lab_id")
    order["ehr_synced_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return order


def _authorize(record: dict[str, Any], order: dict[str, Any], coverage: dict[str, Any], user: str,
               client: HealthcareApiClient) -> None:
    """Background prior authorization: build, save (gets PA ID), submit, save the determination."""
    need = pa.requirement(dx.BY_KEY[order["test_key"]].primary_cpt, coverage)
    if not need["required"]:
        order["prior_auth"] = {"required": False, "status": pa.NOT_REQUIRED, "reason": need["reason"]}
        return
    patient_id = _patient_id(record)
    request = pa.build_request(order, record, coverage, user)
    saved = client.upsert_patient_records(patient_id, "Prior_Authorizations", [request], "PA_ID")
    request = saved[0] if saved else request
    decided = pa.submit(request)
    saved = client.upsert_patient_records(patient_id, "Prior_Authorizations", [decided], "PA_ID")
    order["prior_auth"] = pa.order_summary(saved[0] if saved else decided)


def place_orders(record: dict[str, Any], chart: dict[str, Any] | None, picks: list[dict[str, Any]], *,
                 user: str, priority: str = "Routine", plan_text: str = "",
                 client: HealthcareApiClient | None = None) -> tuple[list[dict[str, Any]], str]:
    """Create orders from picked suggestions (or catalog tests), save them, run prior auth, extend the Plan.

    ``picks`` items: {"test_key", optional "suggestion"}. Returns (new orders, updated plan text).
    """
    client = client or HealthcareApiClient()
    _patient_id(record)
    coverage = primary_coverage(chart) if chart else {}
    diagnoses = dx.encounter_diagnoses(record, chart)
    existing = {order.get("test_key") for order in dx.active_orders(record)}
    created = []
    for pick in picks:
        key = pick["test_key"]
        if key in existing or key not in dx.BY_KEY:
            continue
        suggestion = pick.get("suggestion") or {}
        linked = suggestion.get("linked_dx") or dx.linked_diagnoses(dx.BY_KEY[key], diagnoses)
        order = dx.new_order(
            key, record, reasons=suggestion.get("reasons") or ["Added by clinician"], linked_dx=linked,
            ordered_by=user, priority=priority, source="CDS" if suggestion else "Clinician",
        )
        sync_order(record, order, client)  # gets the ORD number first, so the authorization can cite it
        _authorize(record, order, coverage, user, client)
        sync_order(record, order, client)
        plan_text = append_to_plan(plan_text, dx.plan_line(order, suggestion))
        record.setdefault("diagnostic_orders", []).append(order)
        existing.add(key)
        created.append(order)
    return created, plan_text


def find_order(record: dict[str, Any], order_id: str) -> dict[str, Any] | None:
    return next((order for order in record.get("diagnostic_orders") or [] if order.get("order_id") == order_id), None)


def update_order(record: dict[str, Any], order_id: str, action: str, value: Any = None,
                 client: HealthcareApiClient | None = None) -> dict[str, Any]:
    order = find_order(record, order_id)
    if order is None:
        raise ValueError(f"Order {order_id} is not on this encounter.")
    if action == "perform":
        dx.mark_performed(order)
    elif action == "result":
        dx.record_result(order, value)
    elif action == "cancel":
        dx.cancel_order(order, str(value or ""))
    else:
        raise ValueError(f"Unknown order action: {action}")
    return sync_order(record, order, client)


def refresh_authorizations(record: dict[str, Any], client: HealthcareApiClient | None = None) -> int:
    """Pull decisions staff recorded on the Prior Authorization page back onto the encounter's orders."""
    client = client or HealthcareApiClient()
    orders = [order for order in record.get("diagnostic_orders") or [] if (order.get("prior_auth") or {}).get("pa_id")]
    if not orders:
        return 0
    rows = {row.get("PA_ID"): row for row in client.get_sheet("Prior_Authorizations").get("records", [])}
    changed = 0
    for order in orders:
        row = rows.get(order["prior_auth"]["pa_id"])
        if row and (row.get("Status"), row.get("Auth_Number") or "") != (order["prior_auth"].get("status"), order["prior_auth"].get("auth_number") or ""):
            order["prior_auth"] = pa.order_summary(row)
            changed += 1
    return changed
