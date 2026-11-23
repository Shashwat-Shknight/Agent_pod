"""Role-based dynamic masking of patient data (PHI) in API responses.

Industry pattern: classify data once, decide per role what each class looks like, mask at read time, never
change what is stored. Two layers use the same classification:

  * this module   - application users, per their role, on every API response (operational masking:
                    partial values such as "W*** Y***" or "(***) ***-1002" so people can still work);
  * Snowflake     - GOVERNANCE masking policies (snowflake/migrations/V004__phi_masking.sql) for anyone who
                    queries the database directly with a role other than the application's (analytics:
                    redaction and pseudonyms).

PHI classes follow the HIPAA Safe Harbor identifier families. Whether a role sees a class unmasked is a
normal permission ("PHI: ..." modules, action View) so administrators can tune it in User Access.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

IDENTITY, CONTACT, NARRATIVE = "identity", "contact", "narrative"
# class -> permission module that unmasks it
MODULES = {
    IDENTITY: "PHI: Identity",
    CONTACT: "PHI: Contact details",
    NARRATIVE: "PHI: Clinical narrative",
}
CLASS_LABELS = {
    IDENTITY: "name, date of birth, MRN and member IDs",
    CONTACT: "phone, email and address",
    NARRATIVE: "conversation and note text",
}

# Keys are compared after normalisation: lower case, letters and digits only ("Street_Address" -> "streetaddress").
# The Snowflake function GOVERNANCE.REDACT_PHI carries the same lists (a test keeps them in step).
NAME_KEYS = {"firstname", "lastname", "legalname", "patientname", "displayname", "subscribername", "membername", "patient"}
CONTEXT_NAME_KEYS = {"name"}  # "name" is a patient's name only inside an object that is clearly about a patient
DOB_KEYS = {"dob", "dateofbirth", "birthdate", "patientdob"}
ID_KEYS = {"mrn", "memberid", "groupnumber", "ssn", "subscriberid", "policynumber", "medicaidid", "medicareid"}
CONTACT_KEYS = {"mobile", "phone", "homephone", "workphone", "email", "streetaddress", "address", "addressline1",
                "addressline2", "city", "zip", "zipcode", "postalcode"}
NARRATIVE_KEYS = {"transcript", "transcriptdata", "summary", "patientsummary", "soapnote", "soapdraft", "notesummary",
                  "hpi", "keyhighlights", "chiefcomplaint", "subjective", "excerpt", "entries"}
PATIENT_MARKERS = {"mrn", "patientid", "ehrpatientid", "dob", "memberid", "legalname"}

NARRATIVE_MASK = "[Hidden for your role: clinical narrative]"
REDACTED = "***"
_WORD = re.compile(r"([A-Za-z])[A-Za-z'.-]*")
_YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_DMY = re.compile(r"^\d{1,2}/\d{1,2}/((?:19|20)\d{2})$")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# ---------------------------------------------------------------- mask formats
def mask_name(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return value
    return _WORD.sub(lambda m: m.group(1) + "***", value)


def mask_dob(value: Any, today: date | None = None) -> Any:
    """Year only; anyone 90 or older is aggregated (Safe Harbor)."""
    if not isinstance(value, str) or not value.strip():
        return value
    match = _DMY.match(value.strip()) or _YEAR.search(value)
    if not match:
        return REDACTED
    year = int(match.group(1) if match.re is _DMY else match.group(0))
    limit = (today or date.today()).year - 90
    return f"{limit} or earlier" if year <= limit else f"{year}-**-**"


def mask_identifier(value: Any) -> Any:
    if value is None or value == "":
        return value
    text = str(value)
    return "*" * max(len(text) - 4, 4) + text[-4:] if len(text) > 4 else "****"


def mask_contact(key: str, value: Any) -> Any:
    if not isinstance(value, (str, int)) or value == "":
        return value
    text = str(value)
    if key in {"zip", "zipcode", "postalcode"}:
        return text[:3] + "**" if len(text) >= 5 else "*****"
    if key in {"mobile", "phone", "homephone", "workphone"}:
        digits = re.sub(r"\D", "", text)
        return f"(***) ***-{digits[-4:]}" if len(digits) >= 4 else REDACTED
    if key == "email":
        local, _, domain = text.partition("@")
        tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
        return f"{local[:1]}***@***.{tld}" if domain else REDACTED
    return REDACTED


def redact_text(value: Any) -> Any:
    """Free-text clean-up for viewers who may read narrative but not contact details."""
    if not isinstance(value, str):
        return value
    return _SSN.sub("***-**-****", _PHONE.sub("(***) ***-****", _EMAIL.sub("***@***", value)))


# ---------------------------------------------------------------- policy
class Policy:
    """Which PHI classes are hidden for one viewer."""

    def __init__(self, masked: Iterable[str] = ()):
        self.masked = frozenset(masked)

    @classmethod
    def for_caller(cls, caller: Any) -> "Policy":
        if caller is None:
            return cls()
        return cls(phi for phi, module in MODULES.items() if not caller.can(module, "View"))

    def hides(self, phi: str) -> bool:
        return phi in self.masked

    @property
    def active(self) -> bool:
        return bool(self.masked)

    # ------------------------------------------------------------ apply
    def apply(self, payload: Any) -> Any:
        """Return a masked copy of a JSON-like payload (dicts, lists, scalars). The input is not changed."""
        if not self.active:
            return payload
        return self._walk(payload, parent="")

    def _walk(self, value: Any, parent: str) -> Any:
        if isinstance(value, list):
            return [self._walk(item, parent) for item in value]
        if not isinstance(value, dict):
            return value
        keys = {norm(k) for k in value}
        about_patient = bool(keys & PATIENT_MARKERS)
        out = {}
        for key, item in value.items():
            k = norm(key)
            out[key] = self._field(k, item, about_patient, parent)
        return out

    def _field(self, k: str, item: Any, about_patient: bool, parent: str) -> Any:
        if isinstance(item, dict) and k in NARRATIVE_KEYS:
            return self._walk(item, k)  # e.g. a "summary" object (record summary) is data about the record, not text
        if self.hides(NARRATIVE) and (k in NARRATIVE_KEYS or (k == "text" and parent == "note")):
            if isinstance(item, list):
                return []
            return NARRATIVE_MASK if item not in (None, "") else item
        if self.hides(IDENTITY):
            if isinstance(item, str) and (k in NAME_KEYS or (k in CONTEXT_NAME_KEYS and about_patient)):
                return mask_name(item)
            if k in DOB_KEYS:
                return mask_dob(item)
            if k in ID_KEYS:
                return mask_identifier(item)
        if self.hides(CONTACT):
            if k in CONTACT_KEYS:
                return mask_contact(k, item)
            if k in NARRATIVE_KEYS or (k == "text" and parent == "note"):
                return _redact_deep(item)
        if isinstance(item, (dict, list)):
            return self._walk(item, k)
        return item

    def header(self) -> str:
        return ",".join(sorted(self.masked))


def field_class(api_field: str) -> str | None:
    """The PHI class of an editable patient field (API names), for write checks."""
    k = norm(api_field)
    if k in {"firstname", "lastname", "dob"}:
        return IDENTITY
    if k in CONTACT_KEYS:
        return CONTACT
    return None


def _redact_deep(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_deep(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_deep(item) for key, item in value.items()}
    return value
