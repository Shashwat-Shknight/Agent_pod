"""Claim lifecycle card: submit (837 -> 277CA), post remittance (835), then the patient statement."""

from html import escape

import requests
import streamlit as st

from app.db.settings import use_snowflake
from frontend import rcm, rcm_service
from frontend.api_client import HealthcareApiClient
from frontend.ui.components import badge, card_title, render_banner, render_html_table, render_tiles
from frontend.ui.data import backend_error_message, record_chart, refresh_ehr_cache
from frontend.ui.state import can, current_user, go_to, log_action, save_record

STEPS = ("Generated", "Submitted", "Accepted", "Adjudicated", "Statement", "Closed")
FLASH_KEY = "rcm_lifecycle_flash"


def account_for(encounter_id):
    rows = HealthcareApiClient().get_sheet("Patient_Balance").get("records", [])
    row = next((row for row in rows if str(row.get("Account_ID")) == str(encounter_id)), None)
    return rcm.refresh_account(row) if row else None


def _step_index(account):
    status = account.get("Claim_Status")
    balance = rcm._number(account.get("Delta_Patient_Balance"))
    if status in {"Ready to submit", "Draft"}:
        return 0
    if status == "Rejected":
        return 1
    if status in {"Submitted", "Accepted"}:
        return 2
    if status in {"Paid", "Partially denied", "Denied"}:
        if balance <= 0.004 and account.get("Statement_Date"):
            return 5
        return 4 if account.get("Statement_Date") else 3
    return 0


def _save_claims(updated, claims):
    by_id = {claim["Claim ID"]: claim for claim in updated}
    claims[:] = [by_id.get(claim.get("Claim ID"), claim) for claim in claims]
    if use_snowflake():
        from app.db import billing as billing_repo

        for claim in updated:
            billing_repo.save_claim(claim, actor=current_user()["username"])


def _submit(record, account, selected_claims, claims):
    try:
        posted, updated = rcm_service.submit_claims(account, selected_claims)
        _save_claims(updated, claims)
    except (RuntimeError, requests.exceptions.RequestException, ValueError) as error:
        st.session_state[FLASH_KEY] = ("error", f"Claims were not submitted: {backend_error_message(error)}")
        return
    if record:
        record["rcm_account"] = {**(record.get("rcm_account") or {}), "account_id": posted["Account_ID"], "status": posted["Claim_Status"]}
        save_record(record)
    log_action("claim.submit", f"{posted['Account_ID']}: {', '.join(claim['Claim ID'] for claim in updated)} -> {posted['Claim_Status']}")
    refresh_ehr_cache()
    st.session_state[FLASH_KEY] = (
        ("error", "Clearinghouse rejected the claim (277CA): see the lifecycle below, fix it and resubmit.")
        if posted["Claim_Status"] == "Rejected"
        else ("success", "Claims accepted by the payer (277CA). Post the remittance when the 835 arrives.")
    )


def _remit(record, account, selected_claims, claims):
    try:
        posted, updated, adjudication = rcm_service.post_remittance(record, record_chart(record), account, selected_claims)
        _save_claims(updated, claims)
    except (RuntimeError, requests.exceptions.RequestException, ValueError) as error:
        st.session_state[FLASH_KEY] = ("error", f"Remittance was not posted: {backend_error_message(error)}")
        return
    record["rcm_account"] = {
        "account_id": posted["Account_ID"], "status": posted["Claim_Status"],
        "patient_responsibility": posted["Patient_Responsibility"], "payer_expected": posted["Payer_Expected_Payment"],
    }
    record["rcm_status"] = f"Adjudicated · {posted['Claim_Status']}"
    save_record(record)
    totals = adjudication["totals"]
    log_action("claim.remittance", f"{posted['Account_ID']}: {posted['Claim_Status']} · payer ${totals['payer']:,.2f} · patient ${totals['patient']:,.2f}")
    refresh_ehr_cache()
    message = f"835 posted: {posted['Claim_Status']} · payer paid ${totals['payer']:,.2f} · patient owes ${posted['Delta_Patient_Balance']:,.2f}"
    if posted.get("Statement_Date"):
        message += f" · statement {posted['Statement_Date']}, due {posted['Payment_Due_Date']}"
    st.session_state[FLASH_KEY] = ("success", message)


def render_claim_lifecycle(record, encounter_id, selected_claims, claims):
    with st.container(border=True):
        flash = st.session_state.pop(FLASH_KEY, None)
        try:
            account = account_for(encounter_id)
        except (RuntimeError, requests.exceptions.RequestException) as error:
            card_title("Revenue cycle")
            st.error(f"Patient accounts are unavailable: {backend_error_message(error)}")
            return
        status = (account or {}).get("Claim_Status") or "No account"
        card_title(
            "Revenue cycle",
            "Submit (837) → payer acknowledgement (277CA) → remittance (835) → patient statement → payment",
            trailing=badge(status, {"Paid": "green", "Accepted": "blue", "Rejected": "red", "Denied": "red",
                                    "Partially denied": "amber"}.get(status, "gray")),
        )
        if flash:
            getattr(st, flash[0])(flash[1])
        if account is None:
            st.info("No patient account for this encounter yet. Send it to RCM from Billing & RCM to open one.")
            return
        current = _step_index(account)
        st.markdown(
            '<div class="nc-badge-row">' + "".join(
                badge(("✓ " if index < current else "● " if index == current else "") + step,
                      "green" if index < current else "blue" if index == current else "gray")
                for index, step in enumerate(STEPS)) + "</div>",
            unsafe_allow_html=True,
        )
        render_tiles([
            ("Charges", f"${rcm._number(account.get('Charge')):,.2f}"),
            ("Allowed", f"${rcm._number(account.get('Allowed_Amount')):,.2f}"),
            ("Payer paid" if account.get("Insurance_Adjudication_Date") else "Payer expected",
             f"${rcm._number(account.get('Payer_Paid') or account.get('Payer_Expected_Payment')):,.2f}"),
            ("Patient responsibility", f"${rcm._number(account.get('Patient_Responsibility')):,.2f}"),
            ("Patient balance", f"${rcm._number(account.get('Delta_Patient_Balance')):,.2f}"),
            ("Aging", account.get("Aging_Bucket") or "—"),
        ])
        st.caption(f"Next step: {account.get('Recommended_Action')}")
        if account.get("Denial_Reasons"):
            codes = str(account["Denial_Reasons"]).split(";")
            render_banner("Denied lines: " + ", ".join(codes), " · ".join(f"{code}: {rcm.carc_text().get(code, '')}" for code in codes),
                          tone="danger", icon="&#9888;")
        events = sorted(
            ({"at": event.get("at"), "claim": claim["Claim ID"], "event": event.get("event")}
             for claim in selected_claims for event in claim.get("Lifecycle") or []),
            key=lambda item: str(item["at"]),
        )
        if events:
            render_html_table(["When", "Claim", "Event"],
                              [[escape(str(item["at"])), f"<code>{escape(item['claim'])}</code>", escape(str(item["event"]))] for item in events])
        for claim in selected_claims:
            remittance = claim.get("Remittance") or {}
            if remittance.get("Lines"):
                st.markdown(f'<div class="nc-label" style="margin-top:.6rem">835 · {escape(claim["Claim ID"])} · '
                            f'{escape(remittance.get("Payer Claim Control Number", ""))}</div>', unsafe_allow_html=True)
                render_html_table(
                    ["CPT", "Charge", "Allowed", "Paid", "Patient", "Adjustment codes"],
                    [[f"<code>{escape(line['code'])}</code>", f"${line['charge']:,.2f}", f"${line['allowed']:,.2f}",
                      f"${line['payer']:,.2f}", f"${line['patient']:,.2f}", escape(", ".join(line["carc"]))]
                     for line in remittance["Lines"]],
                )
        can_release = can("RCM Workspace", "Approve")
        with st.container(horizontal=True, gap="small"):
            st.button(
                "Submit claims to payer (837)", key=f"rcm_submit_{encounter_id}", type="primary",
                disabled=not can_release or account["Claim_Status"] not in {"Ready to submit", "Rejected"},
                on_click=_submit, args=(record, account, selected_claims, claims),
                help=None if can_release else "Requires RCM Workspace · Approve.",
            )
            st.button(
                "Post payer remittance (835)", key=f"rcm_remit_{encounter_id}", type="secondary",
                disabled=not can_release or account["Claim_Status"] != "Accepted" or not record,
                on_click=_remit, args=(record, account, selected_claims, claims),
                help="Simulated payer adjudication using the contract and the member's benefits.",
            )
            st.button("Open Patient A/R", key=f"rcm_open_ar_{encounter_id}", type="tertiary", on_click=go_to, args=("rcm_ar",))
        st.caption("Account, benefits and adjudicated claim lines are saved in Snowflake "
                   "(EHR sheets Patient_Balance, Payer_Plan_Benefits, Billing_Claims and BILLING.CLAIMS).")
