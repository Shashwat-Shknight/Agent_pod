"""Patient A/R: balances after the payer, aging, follow-up priority, next best action, and KPIs."""

import pandas as pd
import requests
import streamlit as st

from frontend import rcm, rcm_service
from frontend.api_client import HealthcareApiClient
from frontend.ui.components import card_title, page_title, render_banner, render_tiles
from frontend.ui.data import backend_error_message, refresh_ehr_cache
from frontend.ui.shell import render_page
from frontend.ui.state import can, log_action

PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2, "None": 3}
WORKLIST_COLUMNS = [
    "MRN", "Patient_Name", "Encounter_ID", "Service_Date", "Payer", "Claim_Status", "Charge", "Allowed_Amount",
    "Payer_Paid", "Patient_Responsibility", "Patient_Paid_At_Discharge", "Patient_Payments", "Financial_Assistance",
    "Delta_Patient_Balance", "Statement_Date", "Payment_Due_Date", "Days_Past_Due", "Aging_Bucket",
    "Follow_up_Priority", "Recommended_Action", "Source",
]
FLASH_KEY = "ar_flash"


def _post(account, kind, amount_key):
    amount = float(st.session_state.get(amount_key) or 0.0)
    try:
        updated = rcm.apply_payment(account, amount, kind=kind)
        rcm_service.save_account(updated)
    except (RuntimeError, requests.exceptions.RequestException, ValueError) as error:
        st.session_state[FLASH_KEY] = ("error", f"Not posted: {backend_error_message(error)}")
        return
    label = {"payment": "Patient payment", "assistance": "Financial assistance", "visit": "Point-of-service payment"}[kind]
    log_action(f"ar.{kind}", f"{account['Account_ID']}: ${amount:,.2f}")
    refresh_ehr_cache()
    st.session_state[amount_key] = 0.0
    st.session_state[FLASH_KEY] = ("success", f"{label} of ${amount:,.2f} posted to {account['Account_ID']} · balance "
                                              f"${updated['Delta_Patient_Balance']:,.2f} · {updated['Recommended_Action']}")


def _render_kpis(summary):
    render_tiles([
        ("Outstanding patient A/R", f"${summary['Outstanding patient A/R']:,.2f}"),
        ("Accounts with balance", summary["Accounts with balance"]),
        ("Average balance", f"${summary['Average patient balance']:,.2f}"),
        ("A/R over 90 days", f"{summary['Patient A/R over 90 days %']}%"),
        ("POS collection rate", f"{summary['Point-of-service collection rate %']}%"),
        ("Denial rate", f"{summary['Denial rate %']}%"),
    ])
    render_tiles([
        ("Gross charges", f"${summary['Gross charges']:,.2f}"),
        ("Contractual adjustments", f"${summary['Contractual adjustments']:,.2f}"),
        ("Payer paid", f"${summary['Payer paid']:,.2f}"),
        ("Patient responsibility", f"${summary['Patient responsibility']:,.2f}"),
        ("Net collection rate", f"{summary['Net collection rate %']}%"),
        ("Days in A/R", summary["Days in A/R"]),
    ])


def _render_account_actions(accounts):
    editable = [row for row in accounts if row.get("Source") == "NuuCare RCM"]
    with st.container(border=True):
        card_title("Post to a patient account", "Payments and financial assistance recalculate balance, aging and next action")
        if not editable:
            st.caption("Accounts open here once an encounter is sent to RCM from Billing & RCM.")
            return
        labels = {
            row["Account_ID"]: f"{row.get('MRN') or '—'} · {row.get('Patient_Name') or ''} · {row['Account_ID']} · "
                               f"balance ${rcm._number(row.get('Delta_Patient_Balance')):,.2f}"
            for row in editable
        }
        account_id = st.selectbox("Account", list(labels), format_func=labels.get, key="ar_account")
        account = next(row for row in editable if row["Account_ID"] == account_id)
        render_tiles([
            ("Patient responsibility", f"${rcm._number(account.get('Patient_Responsibility')):,.2f}"),
            ("Paid at visit", f"${rcm._number(account.get('Patient_Paid_At_Discharge')):,.2f}"),
            ("Payments", f"${rcm._number(account.get('Patient_Payments')):,.2f}"),
            ("Financial assistance", f"${rcm._number(account.get('Financial_Assistance')):,.2f}"),
            ("Balance", f"${rcm._number(account.get('Delta_Patient_Balance')):,.2f}"),
            ("Due", account.get("Payment_Due_Date") or "No statement yet"),
        ])
        st.caption(f"Next step: {account.get('Recommended_Action')}")
        allowed = can("RCM Workspace", "Edit")
        left, middle, right = st.columns(3, vertical_alignment="bottom")
        pay_key, aid_key = f"ar_pay_{account_id}", f"ar_aid_{account_id}"
        left.number_input("Amount ($)", min_value=0.0, step=10.0, key=pay_key, disabled=not allowed)
        middle.button("Post patient payment", key=f"{pay_key}_go", type="primary", disabled=not allowed,
                      on_click=_post, args=(account, "payment", pay_key), width="stretch")
        right.button("Apply as financial assistance", key=f"{aid_key}_go", disabled=not allowed,
                     on_click=_post, args=(account, "assistance", pay_key), width="stretch")


def show_patient_ar():
    render_page("rcm_ar")
    page_title("BILLING & RCM", "Patient A/R & Collections",
               "Balances after the payer, with aging, follow-up priority and the next best action per account")
    flash = st.session_state.pop(FLASH_KEY, None)
    if flash:
        getattr(st, flash[0])(flash[1])
    include_history = st.toggle("Include accounts from the chart's existing claim history", value=True, key="ar_history")
    try:
        accounts = rcm_service.load_accounts(include_history)
    except (RuntimeError, requests.exceptions.RequestException) as error:
        st.error(f"Patient accounts are unavailable: {backend_error_message(error)}")
        return
    if not accounts:
        render_banner("No patient accounts yet", "Send a coded encounter to RCM from Billing & RCM to open the first account.")
        return
    summary = rcm.kpis(accounts)
    _render_kpis(summary)

    chart_col, filter_col = st.columns([6, 4], gap="medium")
    with chart_col:
        with st.container(border=True):
            card_title("Patient A/R by aging bucket", "Open patient balances only; days past the statement due date")
            buckets = {key: value for key, value in summary["A/R by aging bucket"].items()
                       if key not in {"No balance", "Credit balance"}}
            st.bar_chart(pd.DataFrame({"Balance ($)": list(buckets.values())}, index=list(buckets.keys())),
                         height=230, x_label="Aging bucket", y_label="Balance ($)")
    with filter_col:
        with st.container(border=True):
            card_title("Worklist filters")
            bucket_filter = st.multiselect("Aging bucket", list(rcm.AGING_ORDER), key="ar_bucket")
            priority_filter = st.multiselect("Follow-up priority", ["High", "Medium", "Low", "None"],
                                             default=["High", "Medium", "Low"], key="ar_priority")
            search = st.text_input("MRN, patient or encounter", key="ar_search", placeholder="e.g. MRN500001")

    rows = [
        row for row in accounts
        if (not bucket_filter or row.get("Aging_Bucket") in bucket_filter)
        and (not priority_filter or row.get("Follow_up_Priority") in priority_filter)
        and (not search or search.lower() in " ".join(str(row.get(key) or "") for key in ("MRN", "Patient_Name", "Encounter_ID")).lower())
    ]
    rows.sort(key=lambda row: (PRIORITY_ORDER.get(row.get("Follow_up_Priority"), 9), -int(rcm._number(row.get("Days_Past_Due"))),
                               -rcm._number(row.get("Delta_Patient_Balance"))))
    with st.container(border=True):
        card_title("Follow-up worklist", f"{len(rows)} of {len(accounts)} account(s) · highest priority first")
        frame = pd.DataFrame([{column: row.get(column) for column in WORKLIST_COLUMNS} for row in rows])
        if not frame.empty:
            money_columns = {column: st.column_config.NumberColumn(column.replace("_", " "), format="$%.2f") for column in (
                "Charge", "Allowed_Amount", "Payer_Paid", "Patient_Responsibility", "Patient_Paid_At_Discharge",
                "Patient_Payments", "Financial_Assistance", "Delta_Patient_Balance")}
            st.dataframe(frame, hide_index=True, width="stretch", column_config=money_columns)
        else:
            st.caption("No account matches the filters.")
    _render_account_actions(accounts)

    with st.expander("KPI definitions"):
        st.dataframe(pd.DataFrame([{"KPI": key, "Definition": value} for key, value in rcm.KPI_DEFINITIONS.items()]),
                     hide_index=True, width="stretch")
    with st.expander("Payer plan benefits snapshots (270/271)"):
        try:
            benefits = HealthcareApiClient().get_sheet("Payer_Plan_Benefits").get("records", [])
        except (RuntimeError, requests.exceptions.RequestException) as error:
            st.caption(f"Unavailable: {backend_error_message(error)}")
            benefits = []
        if benefits:
            st.dataframe(pd.DataFrame(benefits), hide_index=True, width="stretch")
        else:
            st.caption("Benefit snapshots are saved when an encounter is sent to RCM.")
