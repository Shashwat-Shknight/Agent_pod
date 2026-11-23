"""Load synthetic demo data: patients with full EHR charts, a team of users, and consultations at every stage.

    python -m app.scripts.seed_synthetic                 # 30 patients, 24 consultations
    python -m app.scripts.seed_synthetic --patients 50 --consultations 40
    python -m app.scripts.seed_synthetic --reset         # remove earlier synthetic data, then load fresh
    python -m app.scripts.seed_synthetic --remove        # remove synthetic data only

Works on whatever storage .env selects (Snowflake or local JSON). No LLM and no network needed: the AI steps
use scripted output, and everything else goes through the real API (sign-in, permissions, version checks,
coding engine, billing guardrails), so seeded records are exactly what the app itself would have produced.

What is synthetic, so it can always be removed again:
  * patients P200001-P200999 (MRN200001...), emails @synthetic.test, notes tagged "Synthetic (seed)"
  * consultations carrying "synthetic": true
  * user accounts named syn.*
"""

from __future__ import annotations

import argparse
import random
import secrets
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

from app.agent.context import EncounterContext
from app.scripts.synthetic_scenarios import CITIES, FIRST_NAMES, LAST_NAMES, PAYERS, SCENARIOS, STREETS, Scenario

PATIENT_PREFIX = "P2"
FIRST_PATIENT_NUMBER = 200001
USER_PREFIX = "syn."
DATA_TAG = "Synthetic (seed)"
H = {"X-NuuCare-Client": "web"}

# username, full name, role
TEAM = (
    ("syn.doctor", "Dr. Elena Ruiz", "Clinical Reviewer"),
    ("syn.doctor2", "Dr. James Okafor", "Clinical Reviewer"),
    ("syn.coder", "Priya Nair", "Coding Specialist"),
    ("syn.rcm", "Daniel Kim", "RCM Analyst"),
    ("syn.ops", "Grace Liu", "Operations Admin"),
)
# Created only on an empty system (no Super Admin yet), so the app is usable straight after seeding
BOOTSTRAP_ADMIN = ("syn.admin", "Synthetic Administrator", "Super Admin")
# Where each consultation stops in the workflow (cycled so every screen has work in it)
STAGES = ("review", "billed", "signed", "coded", "billed", "draft", "billed", "signed", "coded", "review", "billed", "signed")


def is_synthetic_patient(patient_id: Any) -> bool:
    text = str(patient_id or "")
    return text.startswith(PATIENT_PREFIX) and text[1:].isdigit() and len(text) == 7


# ---------------------------------------------------------------- scripted AI
class ScriptedAgent:
    """Stands in for the LLM: returns the scenario's scribe output and CDS, matched by conversation text."""

    by_transcript = {scenario.transcript.strip(): scenario for scenario in SCENARIOS}

    def _scenario(self, transcript: str) -> Scenario:
        scenario = self.by_transcript.get(str(transcript).strip())
        if scenario is None:
            raise RuntimeError("The synthetic seeder only knows its own scripted conversations.")
        return scenario

    def prepare(self, transcript: str) -> EncounterContext:
        s = self._scenario(transcript)
        return EncounterContext(
            transcript=transcript, soap_note=s.soap(), patient_summary=s.soap(), key_highlights=list(s.highlights),
            chief_complaint=s.subjective.split(".")[0], hpi=s.subjective, objective=s.objective, assessment=s.assessment,
            plan="Pending review of clinical decision support.",
        )

    def add_clinical_decision_support(self, context: EncounterContext) -> EncounterContext:
        s = self._scenario(context.transcript)
        context.retrieved_guidelines = s.guideline_text()
        context.recommendations = s.recommendation_text()
        return context


# ---------------------------------------------------------------- patients and charts
def _iso(day: date) -> str:
    return f"{day.isoformat()}T00:00:00"


def build_patients(count: int, rng: random.Random, today: date) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[Scenario]]:
    """Patient_Master rows, chart rows per sheet, and the scenario each patient was built around."""
    patients, sheets, assigned = [], {name: [] for name in (
        "Problems", "Medications", "Allergies", "Vitals", "Labs", "Insurance", "Care_Team", "Appointments")}, []
    used_names: set[tuple[str, str]] = set()
    for index in range(count):
        scenario = SCENARIOS[index % len(SCENARIOS)]
        sex = scenario.sex or rng.choice(("Female", "Male"))
        while True:
            first, last = rng.choice(FIRST_NAMES[sex]), rng.choice(LAST_NAMES)
            if (first, last) not in used_names:
                used_names.add((first, last))
                break
        age = rng.randint(*scenario.age_range)
        dob = date(today.year - age, rng.randint(1, 12), rng.randint(1, 28))
        city, state, zip_code = rng.choice(CITIES)
        number = FIRST_PATIENT_NUMBER + index
        patient_id, pcp = f"P{number}", rng.choice(("Elena Ruiz, MD", "James Okafor, MD"))
        height = rng.randint(150, 168) if sex == "Female" else rng.randint(165, 190)
        weight = round(height * height / 10000 * rng.uniform(22, 36), 1)
        patients.append({
            "Patient_ID": patient_id, "MRN": f"MRN{number}", "First_Name": first, "Last_Name": last,
            "Legal_Name": f"{first} {last}", "DOB": dob.isoformat(), "Sex_at_Birth": sex,
            "Pronouns": "she/her" if sex == "Female" else "he/him", "Marital_Status": rng.choice(("Single", "Married", "Widowed", "Divorced")),
            "Preferred_Language": rng.choice(("English", "English", "English", "Spanish")), "PCP": pcp, "Record_Status": "Active",
            "Height_cm": height, "Weight_kg": weight, "BMI": round(weight / (height / 100) ** 2, 1),
            "City": city, "State": state, "ZIP": zip_code, "Country": "United States",
            "Street_Address": f"{rng.randint(10, 9999)} {rng.choice(STREETS)}",
            "Mobile": f"(555) {rng.randint(200, 999)}-{rng.randint(1000, 9999)}",
            "Email": f"{first}.{last}.{number}@synthetic.test".lower(),
        })
        assigned.append(scenario)
        tag = f"{patient_id}-{index:03d}"
        for n, (code, name) in enumerate(scenario.problems):
            sheets["Problems"].append({"Problem_ID": f"SYN-PRB-{tag}-{n}", "Patient_ID": patient_id, "ICD_10_CM": code,
                                       "Problem_Name": name, "Clinical_Status": "Active", "Verification_Status": "Confirmed",
                                       "Onset_Date": _iso(today - timedelta(days=rng.randint(200, 3000))), "Provider": pcp,
                                       "Category": "Problem List"})
        for n, (name, dose, frequency, icd) in enumerate(scenario.medications):
            sheets["Medications"].append({"Medication_ID": f"SYN-MED-{tag}-{n}", "Patient_ID": patient_id, "Medication": name,
                                          "Dose": dose, "Route": "Inhaled" if "inhaler" in name.lower() else "Oral",
                                          "Frequency": frequency, "Start_Date": _iso(today - timedelta(days=rng.randint(60, 900))),
                                          "Status": "Active", "Prescriber": pcp, "Indication_ICD_10": icd,
                                          "Dispense": "30 day supply", "Refills": str(rng.randint(0, 5))})
        if scenario.allergy:
            substance, reaction, severity = scenario.allergy
            sheets["Allergies"].append({"Allergy_ID": f"SYN-ALG-{tag}", "Patient_ID": patient_id, "Substance": substance,
                                        "Category": "Drug", "Reaction": reaction, "Severity": severity, "Clinical_Status": "Active",
                                        "Verification_Status": "Confirmed", "Recorded_Date": _iso(today - timedelta(days=900)),
                                        "Recorder": pcp})
        systolic, diastolic, heart_rate, spo2, temperature = scenario.vitals
        sheets["Vitals"].append({"Vital_ID": f"SYN-VIT-{tag}", "Patient_ID": patient_id,
                                 "Observed_Date": _iso(today - timedelta(days=rng.randint(30, 120))),
                                 "Systolic_mmHg": systolic + rng.randint(-6, 6), "Diastolic_mmHg": diastolic + rng.randint(-4, 4),
                                 "Heart_Rate_bpm": heart_rate, "Respiratory_Rate": 16, "SpO2_Percent": spo2, "Temperature_F": temperature,
                                 "Weight_kg": weight, "Height_cm": height, "BMI": round(weight / (height / 100) ** 2, 1),
                                 "Recorded_By": pcp, "Position": "Sitting"})
        for n, (loinc, test, result, unit, reference, flag) in enumerate(scenario.labs):
            sheets["Labs"].append({"Lab_ID": f"SYN-LAB-{tag}-{n}", "Patient_ID": patient_id,
                                   "Result_Date": _iso(today - timedelta(days=rng.randint(20, 100))), "LOINC_Code": loinc,
                                   "Test_Name": test, "Result": result, "Unit": unit, "Reference_Range": reference, "Flag": flag,
                                   "Status": "Final", "Performing_Lab": "Synthetic Reference Lab", "Ordering_Provider": pcp})
        payer, plan = rng.choice(PAYERS)
        sheets["Insurance"].append({"Coverage_ID": f"SYN-COV-{tag}", "Patient_ID": patient_id, "Priority": "Primary",
                                    "Payer_Name": payer, "Member_ID": f"SYN-{number}", "Group_Number": "SYN-GRP-2026",
                                    "Relationship_to_Subscriber": "Self", "Effective_Date": _iso(date(today.year, 1, 1)),
                                    "Termination_Date": _iso(date(today.year, 12, 31)), "Status": "Active", "Plan_Type": plan,
                                    "Assigned_PCP": pcp, "Annual_Deductible_USD": rng.choice((500, 1000, 1500, 2500)),
                                    "Copay_USD": rng.choice((0, 20, 30, 40))})
        sheets["Care_Team"].append({"Care_Team_ID": f"SYN-CT-{tag}", "Patient_ID": patient_id, "Provider": pcp,
                                    "Specialty": "Primary Care", "Role": "PCP", "Status": "Active",
                                    "Start_Date": _iso(date(today.year - 2, 1, 1)), "Organization": "NuuCare Synthetic Clinic"})
        sheets["Appointments"].append({"Appointment_ID": f"SYN-APT-{tag}", "Patient_ID": patient_id,
                                       "Date": _iso(today + timedelta(days=rng.randint(7, 90))), "Time": rng.choice(("08:30", "10:00", "13:30", "15:00")),
                                       "Department": "Family Medicine", "Provider": pcp, "Location": "NuuCare Synthetic Clinic",
                                       "Status": "Scheduled", "Reason": "Follow-up: " + scenario.title, "Booked_Via": "Portal"})
    return patients, sheets, assigned


# ---------------------------------------------------------------- users
def ensure_team(password: str) -> list[str]:
    from app.domain import access_control as ac
    from app.web.context import store_transaction

    created = []
    with store_transaction() as store:
        team = (*TEAM, BOOTSTRAP_ADMIN) if ac.needs_bootstrap(store) else TEAM
        for username, name, role in team:
            if ac.get_user(store, username) is None:
                ac.create_user(store, username, name, f"{username}@synthetic.test", role, password, actor="seed")
                created.append(username)
            else:
                ac.update_user(store, username, "seed", active=True, role=role)
                ac.set_password(store, username, password, actor="seed")
            user = ac.get_user(store, username)
            user["must_change_password"] = False
            user["failed_attempts"], user["locked_until"] = 0, None
    return created


# ---------------------------------------------------------------- removal
def remove_synthetic(log=print) -> dict[str, int]:
    from app.domain import access_control as ac
    from app.domain.settings import APPROVED_SOAP_DIR
    from app.services import ehr_json_service
    from app.web import ehr, records
    from app.web.context import store_transaction

    consultations = 0
    for record in records.list_records():
        if record.get("synthetic") or is_synthetic_patient(ehr.record_patient_id(record)):
            records.delete_record(record["id"], "seed", None)
            soap = APPROVED_SOAP_DIR / f"{record.get('ehr_encounter_id') or record['id']}_soap.json"
            soap.unlink(missing_ok=True)
            consultations += 1
    patient_ids = [p.get("patient_id") for p in ehr_json_service.list_patients() if is_synthetic_patient(p.get("patient_id"))]
    patients = ehr_json_service.delete_patients(patient_ids) if patient_ids else 0
    ehr.invalidate()
    users = 0
    with store_transaction() as store:
        for user in [u for u in store["users"] if u["username"].startswith(USER_PREFIX)]:
            try:
                ac.delete_user(store, user["username"], "seed")
                users += 1
            except ValueError as error:
                log(f"  kept @{user['username']}: {error}")
    _remove_generated_bills(patient_ids)
    return {"consultations": consultations, "patients": patients, "users": users}


def _remove_generated_bills(patient_ids: list[str]) -> None:
    from app.db.settings import use_snowflake

    if not patient_ids or not use_snowflake():
        return  # JSON mode keeps generated bills inside the patient rows that were just removed
    from app.db import client

    placeholders = ", ".join(["%s"] * len(patient_ids))
    client.execute(f"DELETE FROM BILLING.GENERATED_BILLS WHERE PATIENT_ID IN ({placeholders})", tuple(patient_ids))


# ---------------------------------------------------------------- consultations through the real API
class Seeder:
    def __init__(self, password: str, log=print):
        from fastapi.testclient import TestClient

        from app.main import create_app
        from app.web import clinical_routes

        clinical_routes._agent = lambda: ScriptedAgent()  # this process only: the AI steps are scripted
        self.app = create_app()
        self.password = password
        self.log = log
        self.clients: dict[str, Any] = {}
        self._client_class = TestClient

    def client(self, username: str):
        if username not in self.clients:
            client = self._client_class(self.app)  # no "with": the background reference-sync scheduler stays off
            response = client.post("/api/auth/login", headers=H, json={"username": username, "password": self.password})
            if response.status_code != 200 or response.json().get("state") != "ready":
                raise RuntimeError(f"Could not sign in as {username}: {response.text}")
            self.clients[username] = client
        return self.clients[username]

    @staticmethod
    def _ok(response, what: str) -> dict[str, Any]:
        if response.status_code != 200:
            raise RuntimeError(f"{what} failed (HTTP {response.status_code}): {response.text[:300]}")
        return response.json()

    def consultation(self, doctor: str, patient_id: str, scenario: Scenario, stage: str, rng: random.Random) -> str:
        from app.web import records

        api = self.client(doctor)
        draft = self._ok(api.post("/api/scribe/text", headers=H, json={"transcript": scenario.transcript}), "Scribe")
        created = self._ok(api.post("/api/consultations", headers=H, json={
            "draft_id": draft["draft_id"], "patient_id": patient_id, "encounter_type": scenario.encounter_type}), "Create consultation")
        record_id = created["id"]
        records.mutate(record_id, "seed", None, lambda rec: rec.update(synthetic=True))
        if stage == "draft":
            return record_id

        view = self._ok(api.get(f"/api/consultations/{record_id}"), "Open consultation")
        for index, item in enumerate(view["cds"]["items"]):
            decision = {"status": "accepted", "note": ""}
            if index == 1 and rng.random() < 0.3:
                decision = {"status": "modified", "note": item["title"] + " (dose adjusted after discussion with patient)"}
            view = self._ok(api.post(f"/api/consultations/{record_id}/recommendations/{index}/decision", headers=H,
                                     json={"version": view["summary"]["version"], **decision}), "CDS decision")
            view = self._ok(api.post(f"/api/consultations/{record_id}/plan", headers=H,
                                     json={"version": view["summary"]["version"], "line": item["title"]}), "Push to plan")
        if stage == "review":
            return record_id

        self._ok(api.post(f"/api/consultations/{record_id}/sign", headers=H, json={"version": view["summary"]["version"]}), "Sign note")
        if stage == "signed":
            return record_id

        coder = self.client("syn.coder")
        coding = self._ok(coder.get(f"/api/coding/{record_id}"), "Open coding")
        keys = [row["key"] for row in coding["rows"]]
        if keys:
            query = [k for k in keys if "R05" in k][:1] if rng.random() < 0.5 else []
            accept = [k for k in keys if k not in query]
            if accept:
                coding = self._ok(coder.post(f"/api/coding/{record_id}/decisions", headers=H, json={
                    "version": coding["summary"]["version"], "keys": accept, "status": "Accepted",
                    "note": "Supported by the signed note"}), "Accept codes")
            if query:
                self._ok(coder.post(f"/api/coding/{record_id}/decisions", headers=H, json={
                    "version": coding["summary"]["version"], "keys": query, "status": "Query sent",
                    "note": "Acute vs chronic cough? Please clarify duration."}), "Physician query")
        if stage == "coded":
            return record_id

        rcm = self.client("syn.rcm")
        bill = self._ok(rcm.get(f"/api/billing/{record_id}"), "Open billing")
        if bill["can_send"]:
            self._ok(rcm.post(f"/api/billing/{record_id}/send", headers=H,
                              json={"version": bill["summary"]["version"], "attested": True}), "Send to RCM")
        return record_id


def backdate(record_ids: list[str], rng: random.Random, today: date) -> None:
    """Spread the consultations over the last three weeks so dashboards and trends have shape."""
    from app.services import ehr_json_service
    from app.web import ehr, records

    encounter_dates: dict[str, tuple[str, str, str]] = {}
    for position, record_id in enumerate(record_ids):
        days_ago = 0 if position < 3 else rng.randint(1, 21)
        when = datetime.combine(today - timedelta(days=days_ago), datetime.min.time()) + timedelta(
            hours=rng.randint(8, 16), minutes=rng.choice((0, 15, 30, 45)))

        def change(rec, when=when):
            rec["date"], rec["time"] = when.strftime("%d/%m/%Y"), when.strftime("%H:%M")
            if rec.get("approval_date"):
                rec["approval_date"] = when.strftime("%d/%m/%Y")
            if rec.get("bill_generated_at"):
                rec["bill_generated_at"] = (when + timedelta(days=1)).strftime("%d %b %Y %H:%M")
            encounter_dates[rec["id"]] = (rec.get("ehr_encounter_id", ""), rec.get("ehr_note_id", ""), when.date().isoformat())

        records.mutate(record_id, "seed", None, change)
    with ehr_json_service._STORE_LOCK:  # one EHR write for all encounter / note dates
        store = ehr_json_service._read_store()
        wanted = {enc: day for enc, _note, day in encounter_dates.values() if enc}
        notes = {note: day for _enc, note, day in encounter_dates.values() if note}
        for row in store["sheets"]["Encounters"]["records"]:
            if row.get("Encounter_ID") in wanted:
                row["Date"] = _iso(date.fromisoformat(wanted[row["Encounter_ID"]]))
        for row in store["sheets"]["Clinical_Notes"]["records"]:
            if row.get("Note_ID") in notes:
                row["Service_Date"] = _iso(date.fromisoformat(notes[row["Note_ID"]]))
                row["Data_Tag"] = DATA_TAG
        ehr_json_service._write_store(store)
    ehr.invalidate()


# ---------------------------------------------------------------- main
def seed(patient_count: int = 30, consultation_count: int = 24, password: str | None = None, reset: bool = False,
         seed_value: int = 2026, log=print) -> dict[str, Any]:
    from app.services import ehr_json_service
    from app.web import ehr

    if not 1 <= patient_count <= 999:
        raise ValueError("--patients must be between 1 and 999")
    consultation_count = max(0, consultation_count)
    existing = [p for p in ehr_json_service.list_patients() if is_synthetic_patient(p.get("patient_id"))]
    if existing and not reset:
        raise RuntimeError(f"{len(existing)} synthetic patients already exist. Run with --reset to rebuild, or --remove to delete them.")
    if reset:
        removed = remove_synthetic(log)
        log(f"Removed earlier synthetic data: {removed}")

    rng, today = random.Random(seed_value), date.today()
    password = password or (secrets.token_urlsafe(9) + "7a")
    started = time.monotonic()

    log(f"1/4 Creating {patient_count} synthetic patients with charts…")
    patients, sheets, scenarios = build_patients(patient_count, rng, today)
    ehr_json_service.add_records_bulk({"Patient_Master": patients, **sheets})
    ehr.invalidate()

    log("2/4 Creating the synthetic team (syn.*)…")
    created_users = ensure_team(password)

    log(f"3/4 Running {consultation_count} consultations through the API (scribe → CDS → sign → code → bill)…")
    seeder = Seeder(password, log)
    created, failures = [], []
    for index in range(consultation_count):
        patient, scenario = patients[index % len(patients)], scenarios[index % len(patients)]
        stage = STAGES[index % len(STAGES)]
        doctor = "syn.doctor" if index % 3 else "syn.doctor2"
        try:
            created.append(seeder.consultation(doctor, patient["Patient_ID"], scenario, stage, rng))
            log(f"   {index + 1:>3}/{consultation_count}  {patient['Legal_Name']:<22} {scenario.key:<22} → {stage}")
        except Exception as error:  # noqa: BLE001 - keep going; report at the end
            failures.append(f"{patient['Patient_ID']} {scenario.key}: {error}")
            log(f"   {index + 1:>3}/{consultation_count}  FAILED: {error}")

    log("4/4 Spreading visit dates over the last 3 weeks…")
    if created:
        backdate(created, rng, today)
    return {"patients": len(patients), "consultations": len(created), "failures": failures, "password": password,
            "admin_created": BOOTSTRAP_ADMIN[0] in created_users, "seconds": round(time.monotonic() - started, 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load or remove NuuCare synthetic demo data.")
    parser.add_argument("--patients", type=int, default=30, help="synthetic patients to create (default 30)")
    parser.add_argument("--consultations", type=int, default=24, help="consultations to run through the workflow (default 24)")
    parser.add_argument("--password", help="password for the syn.* accounts (default: a random one, printed at the end)")
    parser.add_argument("--reset", action="store_true", help="remove earlier synthetic data first")
    parser.add_argument("--remove", action="store_true", help="only remove synthetic data")
    parser.add_argument("--seed", type=int, default=2026, help="random seed (same seed = same names and dates)")
    args = parser.parse_args(argv)

    from app.db.settings import use_snowflake

    print(f"Storage: {'Snowflake' if use_snowflake() else 'local JSON files'}")
    try:
        if args.remove:
            print(f"Removed: {remove_synthetic()}")
            return 0
        result = seed(args.patients, args.consultations, args.password, args.reset, args.seed)
    except (RuntimeError, ValueError) as error:
        print(f"Stopped: {error}")
        return 1
    print(f"\nDone in {result['seconds']} s: {result['patients']} patients, {result['consultations']} consultations.")
    if result["failures"]:
        print(f"{len(result['failures'])} consultation(s) failed:")
        for failure in result["failures"]:
            print(f"  - {failure}")
    print("\nSign in at http://127.0.0.1:8000 with any of these (same password):")
    for username, name, role in (*TEAM, BOOTSTRAP_ADMIN) if result["admin_created"] else TEAM:
        print(f"  {username:<12} {name:<24} {role}")
    print(f"  password: {result['password']}")
    return 0 if not result["failures"] else 2


if __name__ == "__main__":
    sys.exit(main())
