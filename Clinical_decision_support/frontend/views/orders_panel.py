"""Diagnostic orders card on the CDS workbench: suggested tests, placing orders, results.

Clinicians see test names, reasons and whether a test is due - never CPT/LOINC codes. Prior
authorization runs in the background and shows only as a small status badge.
"""

from html import escape

import requests
import streamlit as st

from frontend import diagnostics as dx
from frontend import orders_service
from frontend import prior_auth as pa
from frontend.ehr_chart import primary_coverage
from frontend.ui.components import badge, card_title, render_html_table
from frontend.ui.data import backend_error_message, refresh_ehr_cache
from frontend.ui.state import can, current_user, log_action, save_record, set_edit_summary

STATUS_TONES = {dx.ORDERED: "amber", dx.PERFORMED: "blue", dx.RESULTED: "green", dx.CANCELLED: "gray"}
PA_TONES = {pa.APPROVED: "green", pa.PENDED: "amber", pa.SUBMITTED: "blue", pa.DENIED: "red", pa.REQUIRED: "amber"}
FLASH_KEY = "orders_flash"


def _flash(kind, message):
    st.session_state[FLASH_KEY] = (kind, message)


def _sync_note(record):
    from frontend.views.consultation import update_ehr_clinical_note

    try:
        update_ehr_clinical_note(record)
    except (RuntimeError, requests.exceptions.RequestException, ValueError):
        pass  # the note is saved on the encounter; the EHR copy catches up on the next save


def _place(record, chart, picks, priority, perform_key):
    try:
        created, plan_text = orders_service.place_orders(
            record, chart, picks, user=current_user()["username"], priority=priority,
            plan_text=st.session_state.get("edit_summary") or record.get("summary") or "",
            perform_in_clinic=bool(st.session_state.get(perform_key, True)),
        )
    except (RuntimeError, requests.exceptions.RequestException, ValueError) as error:
        _flash("error", f"Orders were not placed: {backend_error_message(error)}")
        return
    if not created:
        _flash("info", "Nothing new to order: the selected tests are already on this encounter.")
        return
    set_edit_summary(plan_text)
    record["summary"] = plan_text
    record.setdefault("review_history", []).append(
        {"at": created[0]["ordered_at"], "action": "Ordered: " + ", ".join(order["name"] for order in created)})
    save_record(record)
    refresh_ehr_cache()
    _sync_note(record)
    authorizations = [order for order in created if (order.get("prior_auth") or {}).get("required")]
    log_action("orders.place", f"{record.get('id')}: " + ", ".join(f"{order['order_id']} {order['name']}" for order in created))
    message = f"{len(created)} order(s) placed and added to the Plan"
    if authorizations:
        message += f" · payer authorization requested for {len(authorizations)}"
    _flash("success", message)


def _update(record, order_id, action, value_key=None):
    value = st.session_state.get(value_key) if value_key else None
    try:
        order = orders_service.update_order(record, order_id, action, value)
    except (RuntimeError, requests.exceptions.RequestException, ValueError) as error:
        _flash("error", f"Order {order_id} was not updated: {backend_error_message(error)}")
        return
    save_record(record)
    refresh_ehr_cache()
    log_action(f"orders.{action}", f"{record.get('id')}: {order_id} {order['name']} -> {order['status']}")
    _flash("success", f"{order['name']}: {order['status'].lower()}")


def _perform_in_clinic(record):
    try:
        done = orders_service.perform_in_clinic(record)
    except (RuntimeError, requests.exceptions.RequestException, ValueError) as error:
        _flash("error", f"Could not update the orders: {backend_error_message(error)}")
        return
    if done:
        save_record(record)
        refresh_ehr_cache()
        log_action("orders.perform", f"{record.get('id')}: " + ", ".join(order["order_id"] for order in done))
    _flash("success" if done else "info",
           f"{len(done)} in-clinic test(s) marked performed / collected — now billable" if done else "No open in-clinic tests")


def _refresh_authorizations(record):
    try:
        changed = orders_service.refresh_authorizations(record)
    except (RuntimeError, requests.exceptions.RequestException) as error:
        _flash("error", f"Could not refresh authorizations: {backend_error_message(error)}")
        return
    if changed:
        save_record(record)
    _flash("info", f"{changed} authorization update(s) pulled from the payer queue" if changed else "Authorizations are up to date")


def _pa_badge(order):
    info = order.get("prior_auth") or {}
    if not info.get("required"):
        return ""
    status = info.get("status") or pa.REQUIRED
    return badge(f"Payer auth: {status.lower()}", PA_TONES.get(status, "gray"))


def _render_suggestions(record, chart, editable):
    suggestions = dx.suggest_tests(record, chart)
    coverage = primary_coverage(chart) if chart else {}
    record_key = str(record.get("id"))
    if suggestions:
        st.markdown('<div class="nc-label" style="margin-top:.2rem">SUGGESTED FROM THE ASSESSMENT, CDS AND CHART</div>',
                    unsafe_allow_html=True)
    else:
        st.caption("No tests are implied by this encounter's assessment, CDS recommendations or problem list.")
    picks = []
    for item in suggestions:
        needs_auth = item["prior_auth_candidate"] and pa.requirement(dx.by_key()[item["test_key"]].primary_cpt, coverage)["required"]
        detail = " · ".join(item["reasons"][:2]) + f" — {item['due']['label']}"
        if needs_auth:
            detail += " · payer authorization will be requested automatically"
        checked = st.checkbox(
            f"**{item['name']}**", value=item["preselect"], key=f"dx_pick_{record_key}_{item['test_key']}",
            disabled=not editable,
        )
        st.caption(detail)
        if checked:
            picks.append({"test_key": item["test_key"], "suggestion": item})
    taken = {order.get("test_key") for order in dx.active_orders(record)} | {item["test_key"] for item in suggestions}
    others = [test.key for test in dx.catalog() if test.key not in taken]
    extra = st.multiselect(
        "Add another test", others, format_func=lambda key: f"{dx.by_key()[key].name} · {dx.by_key()[key].category}",
        key=f"dx_extra_{record_key}", disabled=not editable, placeholder="Search the order catalog",
    )
    picks += [{"test_key": key} for key in extra]
    perform_key = f"dx_perform_now_{record_key}"
    st.checkbox(
        "Point-of-care tests are done and specimens collected during this visit", value=True, key=perform_key,
        disabled=not editable,
        help="ECG, point-of-care A1c, urine dip, rapid tests and in-house blood/urine draws become billable now. "
             "Imaging and cardiac studies stay ordered until they are performed.",
    )
    left, right = st.columns([3, 2], vertical_alignment="bottom")
    priority = left.radio("Priority", ["Routine", "STAT"], horizontal=True, key=f"dx_priority_{record_key}", disabled=not editable)
    right.button(
        f"Place {len(picks)} order{'s' if len(picks) != 1 else ''}", key=f"dx_place_{record_key}", type="primary",
        width="stretch", disabled=not editable or not picks, on_click=_place, args=(record, chart, picks, priority, perform_key),
    )


def _render_orders(record, can_result):
    orders = record.get("diagnostic_orders") or []
    if not orders:
        return
    st.markdown('<div class="nc-label" style="margin-top:.8rem">ORDERS ON THIS ENCOUNTER</div>', unsafe_allow_html=True)
    rows = []
    for order in orders:
        test = dx.by_key().get(order.get("test_key"))
        result = order.get("result") or {}
        status = order.get("status")
        label = test.performed_label if test and status == dx.PERFORMED else status
        result_text = ""
        if result:
            result_text = f"{result.get('value')} {result.get('unit') or ''}".strip()
            if result.get("flag") and result["flag"] != "Normal":
                result_text += f" ({result['flag']})"
        rows.append([
            f"<code>{escape(order.get('order_id') or '—')}</code>",
            escape(order.get("name") or ""),
            badge(label, STATUS_TONES.get(status, "gray")) + (" " + badge("STAT", "red") if order.get("priority") == "STAT" else ""),
            escape(result_text or "—"),
            _pa_badge(order) or '<span style="color:#94a3b8">—</span>',
        ])
    render_html_table(["Order", "Test", "Status", "Result", "Authorization"], rows)
    open_orders = [order for order in orders if order.get("status") in (dx.ORDERED, dx.PERFORMED) and order.get("order_id")]
    in_clinic = [order for order in orders if order.get("status") == dx.ORDERED
                 and dx.done_in_clinic(dx.by_key()[order["test_key"]])]
    if in_clinic and can_result:
        st.button(f"Mark {len(in_clinic)} in-clinic test(s) performed / specimens collected", key=f"dx_perform_all_{record.get('id')}",
                  type="primary", on_click=_perform_in_clinic, args=(record,))
    if open_orders and can_result:
        with st.expander("Record specimen collection or results"):
            for order in open_orders:
                test = dx.by_key()[order["test_key"]]
                key = f"dx_result_{order['order_id']}"
                cols = st.columns([3, 2.2, 1.6, 1.6, 1.2], vertical_alignment="bottom")
                cols[0].markdown(f"**{escape(order['name'])}**<br><small>{escape(test.reference or '')}</small>", unsafe_allow_html=True)
                cols[1].text_input(f"Result ({test.unit or 'text'})", key=key, placeholder=test.reference or "Result")
                cols[2].button("Save result", key=f"{key}_save", on_click=_update, args=(record, order["order_id"], "result", key))
                if order["status"] == dx.ORDERED:
                    cols[3].button(test.performed_label, key=f"{key}_perform", on_click=_update,
                                   args=(record, order["order_id"], "perform"))
                    cols[4].button("Cancel", key=f"{key}_cancel", type="tertiary", on_click=_update,
                                   args=(record, order["order_id"], "cancel"))
    if any((order.get("prior_auth") or {}).get("pa_id") for order in orders):
        st.button("Refresh payer authorizations", key=f"dx_pa_refresh_{record.get('id')}", type="tertiary",
                  on_click=_refresh_authorizations, args=(record,))


def render_diagnostic_orders(record, chart, is_approved):
    orders = dx.active_orders(record)
    resulted = sum(order.get("status") == dx.RESULTED for order in orders)
    with st.container(border=True):
        card_title(
            "Diagnostic orders",
            "Tests the plan implies are proposed here, because they are rarely said out loud during the visit",
            trailing=badge(f"{len(orders)} ordered · {resulted} resulted", "green" if orders else "gray"),
        )
        flash = st.session_state.pop(FLASH_KEY, None)
        if flash:
            getattr(st, flash[0])(flash[1])
        if not record.get("ehr_patient_id"):
            st.info("Link this consultation to an EHR patient to place orders.")
            return
        editable = not is_approved and can("Clinical Review", "Edit")
        if is_approved:
            st.caption("The note is signed: new orders need an addendum. Results can still be recorded below.")
        else:
            _render_suggestions(record, chart, editable)
        _render_orders(record, can("Clinical Review", "Edit"))
