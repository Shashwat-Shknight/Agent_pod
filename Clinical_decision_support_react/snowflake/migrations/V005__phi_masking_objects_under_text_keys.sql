-- =====================================================================
-- V005 - REDACT_PHI: an object under a narrative key (e.g. "summary": {...}) is walked, not erased.
-- Same rule as app/domain/masking.py. Policies reference the function by name, so replacing it is enough.
-- =====================================================================
CREATE OR REPLACE SECURE FUNCTION GOVERNANCE.REDACT_PHI(V VARIANT)
RETURNS VARIANT
LANGUAGE JAVASCRIPT
AS
$$
const NAME_KEYS = new Set(["firstname", "lastname", "legalname", "patientname", "displayname", "subscribername", "membername", "patient"]);
const CONTEXT_NAME_KEYS = new Set(["name"]);
const DOB_KEYS = new Set(["dob", "dateofbirth", "birthdate", "patientdob"]);
const ID_KEYS = new Set(["mrn", "memberid", "groupnumber", "ssn", "subscriberid", "policynumber", "medicaidid", "medicareid"]);
const CONTACT_KEYS = new Set(["mobile", "phone", "homephone", "workphone", "email", "streetaddress", "address", "addressline1", "addressline2", "city", "zip", "zipcode", "postalcode"]);
const NARRATIVE_KEYS = new Set(["transcript", "transcriptdata", "summary", "patientsummary", "soapnote", "soapdraft", "notesummary", "hpi", "keyhighlights", "chiefcomplaint", "subjective", "excerpt", "entries"]);
const PATIENT_MARKERS = new Set(["mrn", "patientid", "ehrpatientid", "dob", "memberid", "legalname"]);
const MASK = "[REDACTED]";
const LIMIT = new Date().getFullYear() - 90;

function norm(key) { return String(key).toLowerCase().replace(/[^a-z0-9]/g, ""); }
function empty(v) { return v === null || v === undefined || v === ""; }
function dob(v) {
    if (empty(v)) return v;
    const m = String(v).match(/(19|20)\d{2}/);
    if (!m) return MASK;
    const year = parseInt(m[0], 10);
    return year <= LIMIT ? LIMIT + " or earlier" : String(year);
}
function ident(v) {
    if (empty(v)) return v;
    const t = String(v);
    return t.length > 4 ? "*".repeat(Math.max(t.length - 4, 4)) + t.slice(-4) : "****";
}
function contact(k, v) {
    if (empty(v)) return v;
    if (k === "zip" || k === "zipcode" || k === "postalcode") {
        const t = String(v);
        return t.length >= 5 ? t.slice(0, 3) + "**" : "*****";
    }
    return MASK;
}
function walk(v) {
    if (Array.isArray(v)) return v.map(walk);
    if (v === null || typeof v !== "object") return v;
    const keys = Object.keys(v);
    const normed = keys.map(norm);
    const aboutPatient = normed.some(function (k) { return PATIENT_MARKERS.has(k); });
    const out = {};
    keys.forEach(function (key, i) {
        const k = normed[i];
        const item = v[key];
        const isObject = item !== null && typeof item === "object" && !Array.isArray(item);
        if (NARRATIVE_KEYS.has(k) && isObject) out[key] = walk(item);
        else if (NARRATIVE_KEYS.has(k)) out[key] = empty(item) ? item : MASK;
        else if (typeof item === "string" && (NAME_KEYS.has(k) || (CONTEXT_NAME_KEYS.has(k) && aboutPatient))) out[key] = "[NAME]";
        else if (DOB_KEYS.has(k)) out[key] = dob(item);
        else if (ID_KEYS.has(k)) out[key] = ident(item);
        else if (CONTACT_KEYS.has(k)) out[key] = contact(k, item);
        else out[key] = walk(item);
    });
    return out;
}
return walk(V);
$$;
