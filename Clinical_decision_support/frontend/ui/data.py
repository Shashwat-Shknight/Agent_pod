"""Cached reads from the FastAPI EHR backend, plus backend error formatting."""

import requests
import streamlit as st

from frontend.api_client import HealthcareApiClient

BACKEND_ERRORS = (RuntimeError, requests.exceptions.RequestException, OSError, ValueError)


def backend_error_message(error):
    """Prefer the FastAPI `detail` text over the bare HTTP status line."""
    response = getattr(error, "response", None)
    if response is not None:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if detail:
            return f"{detail} (HTTP {response.status_code})"
    return str(error)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_ehr_patients():
    """Return the EHR patient list, or None when the backend is unavailable."""
    try:
        return HealthcareApiClient().list_patients().get("patients", [])
    except BACKEND_ERRORS:
        return None


@st.cache_data(ttl=60, show_spinner=False)
def fetch_patient_chart(patient_id):
    try:
        return HealthcareApiClient().get_patient_chart(str(patient_id))
    except BACKEND_ERRORS:
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_ehr_export():
    """Whole-EHR export ({"sheets": ...}) for population views, or None when offline."""
    try:
        return HealthcareApiClient().export_patient_data()
    except BACKEND_ERRORS:
        return None


def refresh_ehr_cache():
    """Drop cached EHR reads after a write so every page sees the change."""
    fetch_ehr_patients.clear()
    fetch_patient_chart.clear()
    fetch_ehr_export.clear()


def record_patient_id(record):
    """Internal EHR key (P100001). Used to look records up - never shown to users; show record_mrn()."""
    return str((record or {}).get("ehr_patient_id") or (record or {}).get("patient_id") or "")


def mrn_for_patient(patient_id):
    """MRN of an EHR patient key, from the cached patient list ('' when unknown or offline)."""
    if not patient_id:
        return ""
    for item in fetch_ehr_patients() or []:
        if str(item.get("patient_id") or item.get("Patient_ID") or "") == str(patient_id):
            return str(item.get("MRN") or "")
    return ""


def record_mrn(record, chart=None):
    """The patient's medical record number for a consultation (older records looked up in the EHR)."""
    record = record or {}
    if record.get("mrn"):
        return str(record["mrn"])
    patient = (chart or {}).get("patient") or {}
    if patient.get("MRN"):
        return str(patient["MRN"])
    return mrn_for_patient(record_patient_id(record))


def patient_identifier(record, chart=None):
    """What screens show next to a patient's name."""
    return record_mrn(record, chart) or ("MRN pending" if record_patient_id(record) else "Not linked to the EHR")


def patient_option_label(item):
    """Selector label for an EHR patient: name · MRN · DOB."""
    name = item.get("display_name") or item.get("Legal_Name") or "Unnamed patient"
    dob = str(item.get("DOB") or "").split("T")[0]
    mrn = item.get("MRN") or "MRN pending"
    return f"{name} · {mrn}" + (f" · DOB {dob}" if dob else "")


def record_chart(record):
    patient_id = record_patient_id(record)
    return fetch_patient_chart(patient_id) if patient_id else {}
