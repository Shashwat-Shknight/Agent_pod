"""Paths and constants shared by the frontend."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DB_FILE = PROJECT_ROOT / "clinical_records.json"

EHR_DATA_FILE = PROJECT_ROOT / "Epic_Inspired_USA_50_Patient_Demo.xlsx"

EHR_JSON_FILE = PROJECT_ROOT / "Epic_Inspired_USA_50_Patient_Demo.json"

BILLING_NOTICE = (
    "Gross charges come from the chargemaster. Payer-allowed, payer and patient amounts are estimates from "
    "synthetic contract terms and plan benefits until the payer's remittance (835) is posted. "
    "Professional coding and billing validation is required."
)

ANALYSIS_VERSION = "2"

CODE_MATCHER_VERSION = "5"

ACCESS_STORE_PATH = PROJECT_ROOT / "data" / "access_control.json"

APPROVED_SOAP_DIR = PROJECT_ROOT / "data" / "approved_soap_notes"

SESSION_IDLE_MINUTES = 30
