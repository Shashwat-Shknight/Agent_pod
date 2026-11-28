"""Billing & RCM: performed-service guardrail, draft bill and hand-off to claim generation."""

import re
from datetime import datetime
from html import escape

import pandas as pd
import requests
import streamlit as st

from frontend import diagnostics as dx
from frontend import rcm, rcm_service
from frontend.claim_generation_page import build_ambient_bill, register_generated_bill
from frontend.views.orders_panel import render_charge_capture, sync_from_ehr
from frontend.clinical_text import best_excerpt
from frontend.coding_validation import summarize as summarize_checks
from frontend.ehr_chart import active_drug_allergies, age_from_dob, chart_rows, format_date, latest_first, primary_coverage
from frontend.pdf_documents import archive_pdf, bill_pdf_bytes
from frontend.encounter_pipeline import (
    _procedure_status_inputs,
    build_estimated_hospital_bill,
    classify_procedure_status,
    is_explicitly_performed,
)
from frontend.settings import BILLING_NOTICE
from frontend.ui.components import (
    badge,
    card_title,
    initials,
    page_header,
    page_title,
    render_banner,
    render_html_table,
    render_tiles,
)
from frontend.ui.data import (
    backend_error_message,
    patient_identifier,
    record_chart,
    record_mrn,
    record_patient_id,
    refresh_ehr_cache,
)
from frontend.ui.shell import render_page
from frontend.ui.state import can, current_user, go_to, log_action, open_record, save_record
from frontend.views.coding import (
    VALIDATION_TONES,
    build_code_rows,
    coding_validation,
    encounter_code_results,
    record_documentation,
)


_EM_VISIT_CODES = {f"992{n:02d}" for n in range(2, 16)}  # 99202-99215 office/outpatient visits


def _is_em_visit(code, term):
    """The documented encounter itself is the evidence that the E/M visit happened."""
    return code in _EM_VISIT_CODES or "office visit" in str(term).lower()


def rcm_exceptions(post_checks, unpriced):
    """Open issues that travel with the claim to the RCM work queue instead of blocking the hand-off."""
    exceptions = [
        {"check": check["name"], "detail": check["detail"]} for check in post_checks or [] if check.get("status") == "fail"
    ]
    exceptions += [{"check": "Unpriced service", "detail": f"CPT {code} has no chargemaster rate; not on the claim"}
                   for code in unpriced or []]
    return exceptions


HELD_STATES = {"BLOCKED", "UNVERIFIED", "PENDING", "HELD"}
_ORDER_STATE = {"BILLABLE": ("BILLABLE", "green"), "PENDING": ("PENDING", "amber"), "HELD": ("HELD", "red")}


def order_guardrail_rows(record):
    """Diagnostic orders on the encounter: the order and its result are the performed evidence."""
    rows = []
    for line in dx.billing_lines(record):
        guardrail, tone = _ORDER_STATE[line["state"]]
        order = next((item for item in record.get("diagnostic_orders") or [] if item.get("order_id") == line["order_id"]), {})
        rows.append({
            "candidate": {
                "CPT/HCPCS Code": line["code"],
                "Matched Procedure/Service": line["service"],
                "Extracted Procedure": line["service"],
                "source": "diagnostic_order",
            },
            "code": line["code"],
            "service": line["service"],
            "order": order.get("status") or ("Performed" if line["code"] == dx.VENIPUNCTURE_CPT else "—"),
            "evidence": line["reason"],
            "documentation": f"Order {line['order_id']}" if line["order_id"] else "Specimen collection",
            "guardrail": guardrail,
            "tone": tone,
            "reason": line["reason"],
            "overridden": False,
            "overridable": False,
            "order_line": line,
        })
    return rows


def billing_guardrail_rows(record):
    """Classify each CPT candidate as billable/blocked using performed-status evidence."""
    cpt_results = list(encounter_code_results(record).get("cpt") or [])
    documentation = record_documentation(record)
    decisions = record.get("code_decisions") or {}
    note_text = str(record.get("summary") or "").lower()
    rows = order_guardrail_rows(record)
    ordered_codes = {row["code"] for row in rows}
    for candidate in cpt_results:
        if not isinstance(candidate, dict):
            continue
        code = str(candidate.get("CPT/HCPCS Code") or "").strip()
        term = str(candidate.get("Extracted Procedure") or "")
        if not code or code in ordered_codes:
            continue  # an order for the same test is the authoritative evidence
        status_procedure, status_documentation = _procedure_status_inputs(term, documentation)
        procedure_status = (
            classify_procedure_status(status_procedure, status_documentation) if classify_procedure_status else "unknown"
        )
        performed = bool(is_explicitly_performed and is_explicitly_performed(status_procedure, status_documentation))
        decision = decisions.get(f"CPT:{code}", {}).get("status")
        rejected = decision == "Rejected"
        from_superbill = candidate.get("source") == "ocr_superbill"
        words = [word for word in re.findall(r"[a-z]+", term.lower()) if len(word) > 3]
        in_note = any(word in note_text for word in words)
        is_visit = _is_em_visit(code, term) and bool(documentation.strip())
        if rejected:
            guardrail, tone, reason = "EXCLUDED", "gray", "Rejected by coder"
        elif performed:
            guardrail, tone, reason = "BILLABLE", "green", "Performed and documented"
        elif is_visit:
            guardrail, tone, reason = "BILLABLE", "green", "Encounter documented (E/M visit)"
            procedure_status = "performed"
        elif from_superbill and decision == "Accepted":
            guardrail, tone, reason = "BILLABLE", "green", "Provider superbill (OCR), accepted by coder"
            procedure_status = "superbill"
        elif from_superbill:
            guardrail, tone, reason = "UNVERIFIED", "amber", "Superbill line awaiting coder acceptance"
            procedure_status = "superbill"
        elif procedure_status == "unknown":
            guardrail, tone, reason = "UNVERIFIED", "amber", "Performed status could not be verified"
        else:
            guardrail, tone, reason = "BLOCKED", "red", "Not documented as performed"
        held = guardrail in {"BLOCKED", "UNVERIFIED"}
        override = (record.get("billing_overrides") or {}).get(code) if held else None
        if override:
            guardrail, tone = "BILLABLE", "green"
            reason = f"Confirmed performed by {override.get('by') or 'coder'}"
        rows.append(
            {
                "candidate": candidate,
                "code": code,
                "service": candidate.get("Matched Procedure/Service") or term,
                "order": {
                    "performed": "Performed",
                    "ordered_not_performed": "Ordered",
                    "planned": "Planned",
                    "recommended": "Recommended",
                    "discussed": "Discussed",
                    "historical": "Historical",
                    "negated": "Not performed",
                    "superbill": "Superbill",
                }.get(procedure_status, "Unknown"),
                "evidence": best_excerpt(term, documentation, max_chars=90) or "No performed evidence found",
                "documentation": ("Signed note" if record.get("status") == "Approved" else "Draft note")
                if in_note
                else "Transcript only",
                "guardrail": guardrail,
                "tone": tone,
                "reason": reason,
                # Held by the guardrail but released by a coder; shown as an override choice.
                "overridden": bool(override),
                "overridable": held,
            }
        )
    return rows


def _set_billing_overrides(record, key):
    """Persist the coder's 'confirmed performed' choices for held lines on the encounter."""
    selected = set(st.session_state.get(key) or [])
    previous = record.get("billing_overrides") or {}
    stamp = {"by": current_user()["username"], "at": datetime.now().strftime("%d %b %Y %H:%M")}
    record["billing_overrides"] = {code: previous.get(code) or stamp for code in selected}
    added, removed = selected - set(previous), set(previous) - selected
    save_record(record)
    if added or removed:
        log_action(
            "billing.override",
            f"{record.get('id')}: confirmed {', '.join(sorted(added)) or '—'}; withdrawn {', '.join(sorted(removed)) or '—'}",
        )


def _patient_key(record):
    return record_patient_id(record) or f"local:{str(record.get('name', '')).strip().lower()}"


def _billing_status(record):
    rows = billing_guardrail_rows(record)
    if record.get("bill_generated_at"):
        return "Bill drafted", "green"
    if not rows:
        return "Not coded", "gray"
    blocked = sum(row["guardrail"] in HELD_STATES for row in rows)
    if blocked:
        return f"{blocked} line{'s' if blocked > 1 else ''} held", "red"
    if record.get("status") != "Approved":
        return "Awaiting signature", "amber"
    return "Ready to bill", "amber"


def _render_patient_directory(records):
    patients = {}
    for record in records:
        patients.setdefault(_patient_key(record), []).append(record)
    with st.container(border=True):
        card_title("Patient Directory", f"{len(patients)} patients · local consultations linked to the EHR")
        search = (
            st.text_input("Search", placeholder="Search name / MRN / ID", key="billing_search", label_visibility="collapsed")
            .strip()
            .lower()
        )
        view = (
            st.segmented_control(
                "Filter", ["All", "OPD", "IPD", "Unsynced"], default="All", key="billing_filter", label_visibility="collapsed"
            )
            or "All"
        )
        shown = 0
        for key, patient_records in patients.items():
            latest = patient_records[0]
            chart = record_chart(latest)
            coverage = primary_coverage(chart) if chart else {}
            unsynced = any(not r.get("ehr_encounter_id") for r in patient_records)
            types = {"IPD" if str(r.get("ehr_encounter_type", "")).lower().startswith("inp") else "OPD" for r in patient_records}
            haystack = " ".join([
                str(latest.get("name", "")), key, record_mrn(latest, chart),
                *(str(r.get("id", "")) for r in patient_records), *(str(r.get("ehr_encounter_id") or "") for r in patient_records),
            ]).lower()
            if search and search not in haystack:
                continue
            if (view in {"OPD", "IPD"} and view not in types) or (view == "Unsynced" and not unsynced):
                continue
            shown += 1
            is_selected = st.session_state.billing_patient_key == key
            with st.container(key=f"dir_{'on' if is_selected else 'off'}_{re.sub(r'[^A-Za-z0-9]', '_', key)}", border=True):
                info_col, action_col = st.columns([4, 1.4], vertical_alignment="center")
                meta = " · ".join(part for part in (patient_identifier(latest, chart), coverage.get("Payer_Name", "")) if part)
                info_col.markdown(
                    f'<div class="nc-patient"><div class="nc-initials">{escape(initials(latest.get("name")))}</div>'
                    f"<div><b>{escape(latest.get('name', ''))}</b><small>{escape(meta)}</small></div></div>"
                    + (f'<div style="margin-top:.3rem">{badge("Unsynced", "amber")}</div>' if unsynced else ""),
                    unsafe_allow_html=True,
                )
                action_col.button(
                    "✓" if is_selected else "Open",
                    key=f"billing_open_{key}",
                    type="tertiary",
                    on_click=lambda k=key, r=latest: st.session_state.update(
                        billing_patient_key=k, billing_record_id=r.get("id")
                    ),
                )
        if not shown:
            st.caption("No patients match this filter.")
    return patients


def _render_patient_banner(record, chart):
    patient = (chart or {}).get("patient") or {}
    coverage = primary_coverage(chart) if chart else {}
    allergies = ", ".join(str(row.get("Substance")) for row in active_drug_allergies(chart)) if chart else ""
    synced = bool(record.get("ehr_encounter_id"))
    age = record.get("age") or age_from_dob(patient.get("DOB"))
    badges = []
    if coverage:
        badges.append(badge(f"{coverage.get('Payer_Name', 'Payer')} · {coverage.get('Plan_Type', '')}".strip(" ·"), "blue"))
        badges.append(
            badge(
                f"Eligibility {str(coverage.get('Status', 'unknown')).upper()}",
                "green" if str(coverage.get("Status", "")).lower() == "active" else "amber",
            )
        )
    else:
        badges.append(badge("No coverage on file", "gray"))
    if allergies:
        badges.append(badge(f"Allergy: {allergies}", "red"))
    badges.append(badge("EHR synced" if synced else "Unsynced · local only", "green" if synced else "amber"))
    st.markdown(
        '<div class="nc-banner-card">'
        f'<div class="nc-patient"><div class="nc-initials lg">{escape(initials(record.get("name")))}</div>'
        f'<div><b style="font-size:.95rem">{escape(record.get("name", ""))}</b><small>{escape(patient_identifier(record, chart))} · {escape(record.get("ehr_encounter_id") or record.get("id", ""))} · {escape(str(age))}y · {escape(record.get("gender", ""))}</small></div></div>'
        f'<div class="nc-badge-row">{"".join(badges)}</div></div>',
        unsafe_allow_html=True,
    )


def _render_encounter_list(patient_records, chart, selected_id):
    ehr_encounters = latest_first(chart_rows(chart, "Encounters"), "Date") if chart else []
    local_encounter_ids = {r.get("ehr_encounter_id") for r in patient_records}
    history = [row for row in ehr_encounters if row.get("Encounter_ID") not in local_encounter_ids][:4]
    claims = {row.get("Encounter_ID"): row for row in chart_rows(chart, "Billing_Claims")} if chart else {}
    with st.container(border=True):
        card_title(f"Encounters ({len(patient_records) + len(history)})", "Select an encounter to review its charges")
        widths = [1.5, 1.4, 1.2, 1.6, 1.2, 1.4, 1.3]
        header = st.columns(widths)
        for col, label in zip(header, ("Encounter", "Type", "Date", "Provider", "Note", "Billing status", "Action"), strict=True):
            col.markdown(f'<div class="nc-label" style="margin:0">{label}</div>', unsafe_allow_html=True)
        for record in patient_records:
            is_selected = record.get("id") == selected_id
            billing_text, billing_tone = _billing_status(record)
            with st.container(key=f"encrow_{'sel' if is_selected else 'off'}_{record.get('id')}"):
                cols = st.columns(widths, vertical_alignment="center")
                cols[0].markdown(
                    f'<div class="nc-cell"><b>{escape(record.get("ehr_encounter_id") or record.get("id"))}</b></div>',
                    unsafe_allow_html=True,
                )
                cols[1].markdown(
                    f'<div class="nc-cell">{escape(record.get("ehr_encounter_type") or "Office visit")}</div>',
                    unsafe_allow_html=True,
                )
                cols[2].markdown(
                    f'<div class="nc-cell muted">{escape(format_date(record.get("date")))}</div>', unsafe_allow_html=True
                )
                cols[3].markdown(f'<div class="nc-cell muted">{escape(record.get("doctor", ""))}</div>', unsafe_allow_html=True)
                cols[4].markdown(
                    badge("Signed", "green") if record.get("status") == "Approved" else badge("SOAP draft", "amber"),
                    unsafe_allow_html=True,
                )
                cols[5].markdown(badge(billing_text, billing_tone), unsafe_allow_html=True)
                cols[6].button(
                    "Reviewing" if is_selected else "Review charges",
                    key=f"billing_review_{record.get('id')}",
                    type="secondary",
                    disabled=is_selected,
                    on_click=lambda r=record: st.session_state.update(billing_record_id=r.get("id")),
                )
        for row in history:
            claim = claims.get(row.get("Encounter_ID"), {})
            cols = st.columns(widths, vertical_alignment="center")
            cols[0].markdown(
                f'<div class="nc-cell"><b>{escape(str(row.get("Encounter_ID", "")))}</b></div>', unsafe_allow_html=True
            )
            cols[1].markdown(f'<div class="nc-cell">{escape(str(row.get("Type", "")))}</div>', unsafe_allow_html=True)
            cols[2].markdown(f'<div class="nc-cell muted">{escape(format_date(row.get("Date")))}</div>', unsafe_allow_html=True)
            cols[3].markdown(f'<div class="nc-cell muted">{escape(str(row.get("Provider", "")))}</div>', unsafe_allow_html=True)
            cols[4].markdown(
                badge(row.get("Status") or "—", "green" if str(row.get("Status")).lower() == "completed" else "gray"),
                unsafe_allow_html=True,
            )
            claim_status = claim.get("Claim_Status")
            cols[5].markdown(
                badge(
                    f"Claim {claim_status}" if claim_status else "No claim",
                    "green" if str(claim_status).lower() == "paid" else "gray",
                ),
                unsafe_allow_html=True,
            )
            cols[6].markdown('<span class="nc-meta">EHR history</span>', unsafe_allow_html=True)


@st.cache_data(ttl=30, show_spinner=False)
def _benefit_rows():
    return rcm_service.benefit_rows()


def _carc_badges(codes):
    tones = {"CO-45": "gray", "PR-1": "amber", "PR-2": "amber", "PR-3": "blue", "CO-197": "red", "CO-50": "red", "CO-27": "red"}
    return " ".join(
        f'<span title="{escape(rcm.carc_text().get(code, code))}">{badge(code, tones.get(code, "gray"))}</span>' for code in codes
    )


def _money(value):
    return f"${value:,.2f}"


def _cost_share_detail(line):
    parts = []
    if line["copay"]:
        parts.append(f"copay {_money(line['copay'])}")
    if line["deductible"]:
        parts.append(f"deductible {_money(line['deductible'])}")
    if line["coinsurance"]:
        parts.append(f"coins {_money(line['coinsurance'])}")
    return f"<br><small>{' + '.join(parts)}</small>" if parts else ""


def _render_expected_reimbursement(record, benefits, expected, collect_key):
    """What the payer should allow and pay, and what the patient owes - before the claim is sent."""
    with st.container(border=True):
        eligible = benefits["eligibility_status"] == "Active" or benefits["payer_class"] == "Self-pay"
        card_title(
            "Eligibility & expected reimbursement",
            f"{benefits['payer']} · {benefits['plan_type'] or benefits['payer_class']} · {benefits['contract']}",
            trailing=badge(f"Eligibility {benefits['eligibility_status']}", "green" if eligible else "red"),
        )
        st.caption(
            f"{benefits['eligibility_detail']} Benefits as a 270/271 check returns them; accumulators come from this "
            "member's processed claims. Amounts are estimates from synthetic contract terms."
        )
        render_tiles([
            ("Member ID", benefits["member_id"] or "—"),
            ("Office copay", _money(benefits["copay"])),
            ("Deductible left", f"{_money(benefits['remaining_deductible'])} of ${benefits['annual_deductible']:,.0f}"),
            ("Coinsurance", f"{benefits['coinsurance']:.0%}"),
            ("Out-of-pocket left", "No cap" if benefits["oop_remaining"] is None else _money(benefits["oop_remaining"])),
            ("Contract", f"{benefits['allowed_rate']:.1%} of charges"),
        ])
        if not expected:
            st.caption("Expected reimbursement appears once at least one priced line is billable.")
            return
        render_html_table(
            ["CPT", "Service", "Charge", "Allowed", "Contractual adj.", "Patient", "Payer expected", "Adjustment codes"],
            [
                [
                    f"<code>{escape(line['code'])}</code>",
                    escape(line["description"])
                    + (f"<br><small>Auth {escape(line['auth_number'])}</small>" if line.get("auth_number") else ""),
                    _money(line["charge"]),
                    _money(line["allowed"]),
                    _money(line["contractual"] + line["denied"]),
                    _money(line["patient"]) + _cost_share_detail(line),
                    _money(line["payer"]),
                    _carc_badges(line["carc"]),
                ]
                for line in expected["lines"]
            ],
            ["blocked" if line["denied"] else "" for line in expected["lines"]],
        )
        totals = expected["totals"]
        render_tiles([
            ("Charges", _money(totals["charge"])),
            ("Allowed", _money(totals["allowed"])),
            ("Contractual adjustment", _money(totals["contractual"])),
            ("Would deny", _money(totals["denied"])),
            ("Patient responsibility", _money(totals["patient"])),
            ("Payer expected", _money(totals["payer"])),
        ])
        if expected["denied_lines"]:
            reasons = sorted({code for line in expected["denied_lines"] for code in line["carc"]})
            render_banner(
                f"{len(expected['denied_lines'])} line(s) would be denied: " + ", ".join(reasons),
                " · ".join(f"{code}: {rcm.carc_text().get(code, '')}" for code in reasons)
                + ". Fix the authorization, diagnosis or coverage before sending, or the line goes to the denial work queue.",
                tone="danger",
                icon="&#9888;",
            )
        account = record.get("rcm_account") or {}
        if account:
            st.caption(f"Patient account {account.get('account_id')} · {account.get('status')} · saved to the EHR (Patient_Balance).")
        if collect_key not in st.session_state:
            st.session_state[collect_key] = float(min(benefits["copay"], totals["patient"]))
        st.number_input(
            "Collected at check-out (point-of-service payment)",
            min_value=0.0,
            max_value=float(max(totals["patient"], st.session_state[collect_key], 0.0)),
            step=5.0,
            key=collect_key,
            help="Usually the office copay. Saved on the patient account as Patient_Paid_At_Discharge when you send to RCM.",
            disabled=not can("RCM Workspace", "Edit"),
        )


def show_billing():
    render_page("billing")
    records = st.session_state.records
    if not records:
        page_title("BILLING & RCM", "Charge Review & Billing Guardrail", "Only services performed AND documented reach the bill")
        st.info("No consultations yet. Billing starts from a coded consultation.")
        return
    record = next((r for r in records if r.get("id") == st.session_state.billing_record_id), None)
    if record is None:
        current = st.session_state.current_record
        record = next((r for r in records if current and r.get("id") == current.get("id")), records[0])
        st.session_state.billing_record_id = record.get("id")
    if st.session_state.billing_patient_key != _patient_key(record):
        st.session_state.billing_patient_key = _patient_key(record)

    sync_from_ehr(record, record_chart(record))  # orders, results and payer decisions as the EHR has them
    guardrail = billing_guardrail_rows(record)
    billable = [row for row in guardrail if row["guardrail"] == "BILLABLE"]
    held = [row for row in guardrail if row["guardrail"] in HELD_STATES]
    encounter_type = "IPD" if str(record.get("ehr_encounter_type", "")).lower().startswith("inp") else "OPD"
    attest_key = f"billing_attest_{record.get('id')}"
    bill_rows, bill_total, unpriced = (
        build_estimated_hospital_bill([row["candidate"] for row in billable], encounter_type) if billable else ([], 0.0, [])
    )
    # Chargemaster price for every coded line, billable or held, so held services still show what they cost.
    coded = [row for row in guardrail if row["guardrail"] != "EXCLUDED"]
    prices = {
        line["CPT/HCPCS Code"]: line
        for line in (build_estimated_hospital_bill([row["candidate"] for row in coded], encounter_type)[0] if coded else [])
    }
    held_value = sum(
        float(str(prices.get(row["code"], {}).get("Gross Charge", "")).replace("$", "").replace(",", "") or 0)
        for row in held
        if prices.get(row["code"], {}).get("Pricing Status") == "Priced"
    )
    code_rows = build_code_rows(record, encounter_code_results(record)) if guardrail else []
    post_checks = coding_validation(record, code_rows)[1] if code_rows else []
    post_summary = summarize_checks(post_checks) if code_rows else None
    coding_blocked = bool(post_summary and post_summary["status"] == "fail")
    # Only priced lines go on the claim; validation failures and unpriced codes travel with it as exceptions
    # for the RCM work queue, so a coder-attested bill always reaches RCM.
    priced_rows = [row for row in bill_rows if row.get("Pricing Status") == "Priced"]
    exceptions = rcm_exceptions(post_checks, unpriced)
    # Eligibility, benefits and expected adjudication for the billable lines (payer order of cost share).
    rcm_chart = record_chart(record)
    dx_codes = rcm_service.diagnosis_codes(record)
    order_lines = [row["order_line"] for row in guardrail if row.get("order_line")]
    lines_for_claim = rcm_service.claim_lines(priced_rows, order_lines, dx_codes)
    benefits = rcm_service.encounter_benefits(record, rcm_chart, _benefit_rows())
    expected = rcm_service.adjudicate_lines(lines_for_claim, benefits, dx_codes) if lines_for_claim else None
    collect_key = f"billing_collect_{record.get('id')}"
    if expected and expected["denied_lines"]:
        exceptions = exceptions + [
            {"check": f"Expected denial {', '.join(line['carc'])}", "detail": f"CPT {line['code']} {line['description']}"}
            for line in expected["denied_lines"]
        ]

    actions = page_header(
        "BILLING & RCM",
        "Charge Review & Billing Guardrail",
        "Only services performed AND documented reach the bill",
        action_weight=4.2,
    )
    export = {
        "encounter_id": record.get("ehr_encounter_id") or record.get("id"),
        "patient": record.get("name"),
        "encounter_type": encounter_type,
        "lines": bill_rows,
        "gross_total": bill_total,
        "held_lines": [{"code": row["code"], "reason": row["reason"]} for row in held],
        "notice": BILLING_NOTICE,
    }
    bill_pdf = bill_pdf_bytes(export, record) if bill_rows else b""
    bill_pdf_name = f"bill_{export['encounter_id']}.pdf"
    bill_archive_details = {
        "encounter_id": str(export["encounter_id"] or ""),
        "record_id": str(record.get("id") or ""),
        "patient_name": str(record.get("name") or ""),
        "metadata": {"gross_total": bill_total, "line_count": len(bill_rows), "encounter_type": encounter_type,
                     "held_lines": export["held_lines"]},
    }
    actions.download_button(
        "Download draft (PDF)",
        data=bill_pdf,
        file_name=bill_pdf_name,
        mime="application/pdf",
        icon=":material/picture_as_pdf:",
        disabled=not bill_rows,
        key="billing_download",
        on_click=archive_pdf,
        args=("DRAFT_BILL", bill_pdf_name, bill_pdf),
        kwargs=bill_archive_details,
    )

    def send_to_rcm():
        selected_codes = [row["CPT/HCPCS Code"] for row in priced_rows]
        estimate = {
            "record_id": record.get("id"),
            "encounter_type": encounter_type,
            "code_results_signature": None,
            "selected_cpt_codes": selected_codes,
            "rows": priced_rows,
            "total": bill_total,
            "unpriced_codes": unpriced,
            "rcm_exceptions": exceptions,
        }
        coverage = rcm_service.primary_coverage(rcm_chart) if rcm_chart else {}
        estimate.update({
            "claim_lines": lines_for_claim,
            "diagnosis_codes": dx_codes,
            "mrn": record.get("mrn") or "",
            "coverage": {key: coverage.get(key) for key in (
                "Payer_Name", "Plan_Type", "Member_ID", "Group_Number", "Relationship_to_Subscriber")},
            "expected": {"totals": expected["totals"], "status": expected["status"]} if expected else None,
        })
        st.session_state.estimated_hospital_bill = estimate
        # Saved with the encounter so Claim Generation finds it in any later session.
        record["bill_estimate"] = estimate
        if expected:
            try:
                account = rcm_service.save_send_to_rcm(
                    record, rcm_chart, benefits, expected,
                    collected_at_visit=float(st.session_state.get(collect_key) or 0.0),
                )
                record["rcm_account"] = {
                    "account_id": account["Account_ID"],
                    "status": account["Claim_Status"],
                    "patient_responsibility": account["Patient_Responsibility"],
                    "payer_expected": account["Payer_Expected_Payment"],
                }
            except (RuntimeError, requests.exceptions.RequestException, ValueError) as error:
                exceptions.append({"check": "Patient account", "detail": f"Not saved to the EHR: {backend_error_message(error)}"})
            _benefit_rows.clear()
            refresh_ehr_cache()
        bill = build_ambient_bill(record, estimate, encounter_code_results(record))
        bill["rcm_exceptions"] = exceptions
        register_generated_bill(bill, source=encounter_type)
        record["bill_generated_at"] = datetime.now().strftime("%d %b %Y %H:%M")
        record["bill_released_by"] = current_user()["username"]
        record["rcm_exceptions"] = exceptions
        record["rcm_status"] = f"RCM review · {len(exceptions)} exception(s)" if exceptions else "Sent to RCM"
        save_record(record)
        # The released bill is archived as a PDF in the database (BILLING.BILL_DOCUMENTS).
        archive_pdf("DRAFT_BILL", bill_pdf_name, bill_pdf_bytes(export, record), **bill_archive_details)
        detail = f"{record.get('id')}: {', '.join(selected_codes)} · ${bill_total:,.2f}"
        if exceptions:
            detail += " · exceptions: " + "; ".join(item["check"] for item in exceptions)
        log_action("claim.send_with_exceptions" if exceptions else "claim.send", detail)
        go_to("claim_generation")

    can_release = can("RCM Workspace", "Approve")
    attested = st.session_state.get(attest_key, False)
    signed = record.get("status") == "Approved"
    actions.button(
        "Send to RCM (claim draft) ➤",
        key="accent_billing_send",
        on_click=send_to_rcm,
        disabled=not priced_rows or not attested or not can_release or not signed or not dx_codes,
        help=(
            "Your role cannot release claims (RCM Workspace · Approve)."
            if not can_release
            else "The clinical note is not signed yet: a claim needs an authenticated note."
            if not signed
            else "No accepted diagnosis: the coder accepts at least one ICD-10-CM code in Coding & HCC."
            if not dx_codes
            else "No priced, billable line yet: confirm performed services with the coder override below."
            if not priced_rows
            else "Tick the coder attestation below."
            if not attested
            else f"Sends the claim with {len(exceptions)} exception(s) for RCM review." if exceptions else None
        ),
    )

    directory_col, main_col = st.columns([2.5, 7.5], gap="medium")
    with directory_col:
        patients = _render_patient_directory(records)
    patient_records = patients.get(_patient_key(record), [record])
    if record not in patient_records:
        record = patient_records[0]
    with main_col:
        chart = record_chart(record)
        _render_patient_banner(record, chart)
        _render_encounter_list(patient_records, chart, record.get("id"))
        if not guardrail:
            render_banner(
                "This encounter has not been coded yet",
                "Run the Coding & HCC workspace to extract CPT/HCPCS candidates.",
                tone="warn",
                icon="&#9888;",
            )
            st.button(
                "Open Coding & HCC →", key="billing_open_coding", type="primary", on_click=open_record, args=(record, "codes")
            )
            return
        if post_summary:
            label, tone = VALIDATION_TONES[post_summary["status"]]
            banner_tone = {"pass": "ok", "warn": "warn", "fail": "danger"}[post_summary["status"]]
            detail = (
                f"You can still send to RCM: the {post_summary['fails']} open issue(s) go with the claim as exceptions "
                "for the RCM work queue, or resolve them in Coding & HCC first."
                if coding_blocked
                else f"Validation score {post_summary['score']}/100 · review details in Coding & HCC."
            )
            render_banner(
                f"Post-coding validation: {post_summary['label']}",
                detail,
                tone=banner_tone,
                icon="&#10003;" if post_summary["status"] == "pass" else "&#9888;",
                trailing=badge(label, tone),
            )
            if coding_blocked:
                st.button(
                    "Resolve in Coding & HCC →",
                    key="billing_fix_coding",
                    type="secondary",
                    on_click=open_record,
                    args=(record, "codes"),
                )
        pending_orders = [row for row in held if row["guardrail"] == "PENDING"]
        other_held = [row for row in held if row["guardrail"] != "PENDING"]
        if pending_orders:
            render_banner(
                f"{len(pending_orders)} ordered test(s) not performed yet — " + ", ".join(row["code"] for row in pending_orders),
                "A test is billed once it is performed or its specimen collected (billing an unperformed service is a "
                "false claim). Confirm them in Charge capture below; confirmed lines join this bill automatically.",
                tone="warn",
                icon="&#9432;",
                trailing=badge("AWAITING PERFORMANCE", "amber"),
            )
        render_charge_capture(record, can("RCM Workspace", "Edit"))
        if other_held:
            codes = ", ".join(row["code"] for row in other_held)
            render_banner(
                f"{len(other_held)} line{'s' if len(other_held) > 1 else ''} held — {codes}",
                " · ".join(sorted({row["reason"] for row in other_held}))[:400],
                tone="danger",
                icon="&#9888;",
                trailing=badge("BLOCKED", "red"),
            )
        with st.container(border=True):
            card_title(
                "Performed-Service Guardrail",
                f"{record.get('ehr_encounter_id') or record.get('id')} · order, evidence and documentation reconciliation",
                trailing=badge(f"{len(billable)} billable · {len(held)} held", "amber" if held else "green"),
            )
            render_html_table(
                ["CPT", "Service", "Order", "Performed evidence", "Documentation", "Charge", "Guardrail"],
                [
                    [
                        f"<code>{escape(row['code'])}</code>",
                        escape(prices.get(row["code"], {}).get("Service Description") or row["service"]),
                        escape(row["order"]),
                        escape(row["evidence"]),
                        escape(row["documentation"]),
                        escape(str(prices.get(row["code"], {}).get("Gross Charge") or "—")),
                        badge(row["guardrail"], row["tone"]),
                    ]
                    for row in guardrail
                ],
                ["blocked" if row["guardrail"] == "BLOCKED" else "" for row in guardrail],
            )
            overridable = [row for row in guardrail if row["overridable"]]
            if overridable:
                override_key = f"billing_override_{record.get('id')}"
                if override_key not in st.session_state:
                    st.session_state[override_key] = [row["code"] for row in overridable if row["overridden"]]
                labels = {
                    row["code"]: f"{row['code']} · {prices.get(row['code'], {}).get('Service Description') or row['service']}"
                    f" · {prices.get(row['code'], {}).get('Gross Charge') or 'Unpriced'}"
                    for row in overridable
                }
                st.multiselect(
                    "Coder override: confirm held services that were actually performed",
                    options=list(labels),
                    format_func=labels.get,
                    key=override_key,
                    on_change=_set_billing_overrides,
                    args=(record, override_key),
                    disabled=not can("RCM Workspace", "Edit"),
                    placeholder="Select services to release to the bill",
                    help="Use only when the service was performed but the note does not say so. "
                    "Your username and time are saved with the encounter and in the audit log.",
                )
            attestation = (
                "I reviewed these CPT/HCPCS codes and confirm the services were performed and documented for this encounter."
            )
            if exceptions:
                attestation += f" I acknowledge {len(exceptions)} open exception(s) that go to the RCM work queue for review."
                render_banner(
                    f"{len(exceptions)} exception(s) will travel with the claim",
                    " · ".join(f"{item['check']}: {item['detail']}" for item in exceptions)[:600],
                    tone="warn",
                    icon="&#9888;",
                )
            st.checkbox(attestation, key=attest_key, disabled=not can("RCM Workspace", "Edit"))
        with st.container(border=True):
            coverage = primary_coverage(chart) if chart else {}
            bill_id = f"BILL-{re.sub(r'[^A-Za-z0-9]', '', str(record.get('ehr_encounter_id') or record.get('id')))[-6:]}"
            card_title(
                f"Bill {bill_id}",
                " · ".join(
                    part
                    for part in (
                        "Professional claim",
                        coverage.get("Payer_Name", ""),
                        coverage.get("Plan_Type", ""),
                        "Outpatient" if encounter_type == "OPD" else "Inpatient",
                    )
                    if part
                ),
                trailing=badge(
                    "DRAFTED" if record.get("bill_generated_at") else "DRAFT",
                    "green" if record.get("bill_generated_at") else "amber",
                ),
            )
            dx_codes = [
                str(item.get("ICD-10 Code"))
                for item in (record.get("code_results") or {}).get("icd10", [])
                if isinstance(item, dict) and item.get("ICD-10 Code")
            ]
            render_tiles(
                [
                    ("Gross", f"${bill_total:,.2f}"),
                    ("Billable lines", len(bill_rows)),
                    ("Held lines", len(held)),
                    ("Held value", f"${held_value:,.2f}"),
                    ("Unpriced", len(unpriced)),
                    ("Diagnosis codes", ", ".join(dx_codes[:3]) or "—"),
                ]
            )
            if bill_rows:
                st.dataframe(pd.DataFrame(bill_rows), hide_index=True, width="stretch")
            if held:
                st.markdown('<div class="nc-label" style="margin-top:.6rem">Held — not in gross</div>', unsafe_allow_html=True)
                st.dataframe(
                    pd.DataFrame(
                        [{**prices.get(row["code"], {"CPT/HCPCS Code": row["code"]}), "Held reason": row["reason"]} for row in held]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            if unpriced:
                render_banner(
                    "Unpriced codes: " + ", ".join(unpriced),
                    "No chargemaster rate was found; they are excluded from the gross.",
                    tone="warn",
                    icon="&#9888;",
                )
            st.caption(BILLING_NOTICE)
        _render_expected_reimbursement(record, benefits, expected, collect_key)
        _render_archived_bills(record)


def _render_archived_bills(record):
    """Bill PDFs stored in the database for this consultation (BILLING.BILL_DOCUMENTS)."""
    from app.db.settings import use_snowflake

    if not use_snowflake():
        return
    from app.db import billing as billing_repo
    from app.db.client import StorageError

    with st.container(border=True):
        card_title("Archived bill PDFs", "Stored in Snowflake · BILLING.BILL_DOCUMENTS")
        try:
            documents = billing_repo.list_documents(record_id=str(record.get("id") or ""), document_type="DRAFT_BILL")
        except StorageError as error:
            st.error(f"Could not load archived bills: {error}")
            return
        if not documents:
            st.caption("No bill PDFs archived yet. Download the draft or send it to RCM to archive one.")
            return
        for document in documents:
            created = document["created_at"]
            label = f"{document['file_name']} · {created:%d %b %Y %H:%M} UTC · {document['created_by']} · {document['size_bytes'] / 1024:.1f} KB"
            fetch_key = f"billing_archive_{document['document_id']}"
            left, right = st.columns([7, 3], vertical_alignment="center")
            left.markdown(f"<span style='font-size:.85rem'>{escape(label)}</span>", unsafe_allow_html=True)
            if st.session_state.get(fetch_key):
                stored = billing_repo.get_document(document["document_id"])
                if stored:
                    right.download_button("Save PDF", data=stored[1], file_name=stored[0], mime="application/pdf",
                                          key=f"{fetch_key}_dl", icon=":material/download:")
            else:
                right.button("Fetch from database", key=f"{fetch_key}_btn", icon=":material/cloud_download:",
                             on_click=lambda key=fetch_key: st.session_state.__setitem__(key, True))
