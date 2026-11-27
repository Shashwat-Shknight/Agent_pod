"""Diagnostic orders: which tests the plan implies, why, whether they are due, and when they are billable.

Doctors rarely say "I'll order an HbA1c" out loud, so the ambient scribe never hears it and the test
never reaches the note, the EHR or the bill. This module closes that gap without an LLM: it reads the
assessment and plan, the CDS recommendations, the coded diagnoses and the chart (problem list and the
last result of each test) and proposes the tests, each with its reason and a due/not-due check.

Orders then follow a real lifecycle - Ordered -> Performed (specimen collected / study done) -> Resulted -
and only performed or resulted tests become billable lines. Clinicians see test names, never codes:
CPT/LOINC travel with the order for coding and billing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

ORDERED, PERFORMED, RESULTED, CANCELLED = "Ordered", "Performed", "Resulted", "Cancelled"
ACTIVE_STATUSES = (ORDERED, PERFORMED, RESULTED)
BILLABLE_STATUSES = (PERFORMED, RESULTED)
VENIPUNCTURE_CPT = "36415"
IN_HOUSE_LAB = "NuuCare In-house Laboratory"


# Component LOINCs that mean a panel was drawn (a chart may store LDL or hemoglobin instead of the panel code).
PANEL_COMPONENTS = {
    "lipid": ("2093-3", "13457-7", "2089-1", "2085-9", "2571-8"),
    "bmp": ("2345-7", "2160-0", "2823-3", "2951-2"),
    "cmp": ("1742-6", "1920-8", "1751-7"),
    "cbc": ("718-7", "6690-2", "777-3", "4544-3"),
    "uacr": ("14959-1", "32294-1"),
    "a1c": ("17856-6",),
}


@dataclass(frozen=True)
class DiagnosticTest:
    key: str
    name: str
    category: str  # Laboratory, Imaging, Cardiology, Pulmonary, Screening
    cpts: tuple[str, ...]
    loinc: str = ""
    specimen: str = ""  # Blood / Urine / Swab, empty for studies
    unit: str = ""
    low: float | None = None
    high: float | None = None
    reference: str = ""
    indications: tuple[str, ...] = ()  # ICD-10-CM prefixes that support medical necessity
    keywords: tuple[str, ...] = ()  # the test itself named in CDS / plan text
    conditions: tuple[str, ...] = ()  # conditions in the assessment that call for the test
    interval_days: int = 0  # minimum days between repeats (0 = no frequency limit)
    chronic_monitoring: bool = False  # may be proposed from the problem list alone
    point_of_care: bool = False  # result available during the visit
    prior_auth: bool = False  # payer authorization commonly required (commercial / Medicare Advantage)
    aliases: tuple[str, ...] = field(default=())

    @property
    def history_loincs(self) -> tuple[str, ...]:
        """The test's own LOINC plus component results that show the panel was drawn."""
        return (self.loinc, *PANEL_COMPONENTS.get(self.key, ())) if self.loinc else ()

    @property
    def primary_cpt(self) -> str:
        return self.cpts[0]

    @property
    def performed_label(self) -> str:
        return "Specimen collected" if self.specimen else "Performed"


CATALOG: tuple[DiagnosticTest, ...] = (
    DiagnosticTest(
        "a1c", "Hemoglobin A1c", "Laboratory", ("83036",), "4548-4", "Blood", "%", 4.0, 5.6, "4.0-5.6 %",
        ("E10", "E11", "E13", "O24", "R73", "Z13.1"), ("a1c", "hba1c", "hemoglobin a1c", "glycated hemoglobin"),
        ("diabetes", "prediabetes", "hyperglycemia", "glycemic control"), 90, True, True,
    ),
    DiagnosticTest(
        "lipid", "Lipid panel", "Laboratory", ("80061",), "24331-1", "Blood", "mg/dL LDL", None, 100.0, "LDL < 100 mg/dL",
        ("E78", "E11", "E10", "I10", "I25", "I63", "Z13.220"), ("lipid panel", "lipid profile", "fasting lipid", "ldl", "cholesterol panel"),
        ("hyperlipidemia", "dyslipidemia", "high cholesterol", "hypercholesterolemia", "statin"), 180, True,
    ),
    DiagnosticTest(
        "bmp", "Basic metabolic panel", "Laboratory", ("80048",), "51990-0", "Blood", "mg/dL creatinine", 0.6, 1.3,
        "Creatinine 0.6-1.3 mg/dL", ("I10", "N18", "E11", "E10", "E86", "I50", "Z79.899"),
        ("basic metabolic panel", "bmp", "electrolytes", "renal function", "kidney function", "serum creatinine", "egfr", "potassium level"),
        ("hypertension", "chronic kidney disease", "ckd", "dehydration", "heart failure"), 180, True,
    ),
    DiagnosticTest(
        "cmp", "Comprehensive metabolic panel", "Laboratory", ("80053",), "24323-8", "Blood", "U/L ALT", 7.0, 56.0,
        "ALT 7-56 U/L", ("K70", "K75", "K76", "R74", "E11", "R53"),
        ("comprehensive metabolic panel", "cmp", "liver function test", "liver enzymes", "lfts"),
        ("fatty liver", "hepatitis", "elevated liver enzymes"), 180,
    ),
    DiagnosticTest(
        "cbc", "Complete blood count with differential", "Laboratory", ("85025",), "58410-2", "Blood", "g/dL hemoglobin",
        12.0, 17.5, "Hemoglobin 12.0-17.5 g/dL", ("D50", "D51", "D53", "D63", "D64", "R53", "R50", "J18", "R71"),
        ("cbc", "complete blood count", "blood count", "hemoglobin level", "white count"),
        ("anemia", "fatigue", "pneumonia", "infection", "bleeding", "fever"), 30,
    ),
    DiagnosticTest(
        "tsh", "Thyroid stimulating hormone (TSH)", "Laboratory", ("84443",), "3016-3", "Blood", "mIU/L", 0.4, 4.0,
        "0.4-4.0 mIU/L", ("E03", "E05", "E06", "R53", "R63", "F32", "F41"), ("tsh", "thyroid function", "thyroid panel"),
        ("hypothyroidism", "hyperthyroidism", "thyroid", "unexplained weight", "fatigue"), 180, True,
    ),
    DiagnosticTest(
        "uacr", "Urine albumin-to-creatinine ratio", "Laboratory", ("82043", "82570"), "9318-7", "Urine", "mg/g", None,
        30.0, "< 30 mg/g", ("E11", "E10", "E13", "N18", "I10"),
        ("albumin-to-creatinine", "albumin to creatinine", "uacr", "microalbumin", "urine albumin", "albuminuria"),
        ("diabetic nephropathy", "diabetic kidney", "chronic kidney disease"), 365, True,
    ),
    DiagnosticTest(
        "ua", "Urinalysis", "Laboratory", ("81003",), "24357-6", "Urine", "", None, None, "Negative",
        ("N39.0", "R30", "R31", "R35", "N18", "N30"), ("urinalysis", "urine dipstick", "urine analysis"),
        ("urinary tract infection", "uti", "dysuria", "hematuria", "frequent urination"), 0, False, True,
    ),
    DiagnosticTest(
        "b12", "Vitamin B12", "Laboratory", ("82607",), "2132-9", "Blood", "pg/mL", 200.0, 900.0, "200-900 pg/mL",
        ("D51", "E53.8", "G62", "G63", "E11.4", "Z79.84", "R20", "R53"), ("vitamin b12", "b12 level", "cobalamin"),
        ("neuropathy", "numbness", "tingling", "macrocytic"), 365,
    ),
    DiagnosticTest(
        "ferritin", "Ferritin", "Laboratory", ("82728",), "2276-4", "Blood", "ng/mL", 30.0, 400.0, "30-400 ng/mL",
        ("D50", "D64", "R53", "E83.1"), ("ferritin", "iron studies", "iron panel"), ("iron deficiency", "anemia"), 90,
    ),
    DiagnosticTest(
        "vitd", "Vitamin D, 25-hydroxy", "Laboratory", ("82306",), "62292-8", "Blood", "ng/mL", 30.0, 100.0, "30-100 ng/mL",
        ("E55", "M81", "M80", "E21"), ("vitamin d level", "25-hydroxy", "vitamin d"), ("osteoporosis", "osteopenia"), 365,
    ),
    DiagnosticTest(
        "psa", "Prostate specific antigen (PSA)", "Laboratory", ("84153",), "2857-1", "Blood", "ng/mL", None, 4.0, "< 4.0 ng/mL",
        ("N40", "R97.2", "Z12.5", "C61"), ("psa", "prostate specific antigen", "prostate-specific antigen"),
        ("benign prostatic hyperplasia", "bph", "prostate"), 365,
    ),
    DiagnosticTest(
        "troponin", "Troponin, quantitative", "Laboratory", ("84484",), "10839-9", "Blood", "ng/mL", None, 0.04, "< 0.04 ng/mL",
        ("R07", "I20", "I21", "I24"), ("troponin", "cardiac enzymes"), ("acute coronary syndrome",), 0,
    ),
    DiagnosticTest(
        "bnp", "B-type natriuretic peptide (BNP)", "Laboratory", ("83880",), "30934-4", "Blood", "pg/mL", None, 100.0,
        "< 100 pg/mL", ("I50", "R06.0", "R60", "I11.0"), ("bnp", "natriuretic peptide"), ("heart failure", "edema"), 0,
    ),
    DiagnosticTest(
        "ddimer", "D-dimer", "Laboratory", ("85379",), "48065-7", "Blood", "ng/mL FEU", None, 500.0, "< 500 ng/mL FEU",
        ("I26", "I82", "R07", "R06"), ("d-dimer", "d dimer"), ("pulmonary embolism", "deep vein thrombosis", "dvt"), 0,
    ),
    DiagnosticTest(
        "inr", "Prothrombin time / INR", "Laboratory", ("85610",), "6301-6", "Blood", "INR", 2.0, 3.0,
        "2.0-3.0 on warfarin", ("Z79.01", "I48", "I26", "I82", "D68"), ("inr", "pt/inr", "prothrombin time"),
        ("warfarin", "anticoagulation", "atrial fibrillation"), 28, True, True,
    ),
    DiagnosticTest(
        "hcv", "Hepatitis C antibody", "Screening", ("86803",), "16128-1", "Blood", "", None, None, "Nonreactive",
        ("Z11.59", "B18.2", "Z72.89"), ("hepatitis c", "hcv antibody", "hcv screening"), ("hepatitis c",), 0,
    ),
    DiagnosticTest(
        "strep", "Rapid strep A test", "Laboratory", ("87880",), "78012-2", "Swab", "", None, None, "Negative",
        ("J02", "J03"), ("rapid strep", "strep test", "strep a"), ("pharyngitis", "sore throat", "tonsillitis"), 0, False, True,
    ),
    DiagnosticTest(
        "flu", "Rapid influenza test", "Laboratory", ("87804",), "80382-5", "Swab", "", None, None, "Negative",
        ("J09", "J10", "J11", "R50"), ("influenza test", "flu test", "rapid influenza", "flu swab"), ("influenza",), 0, False, True,
    ),
    DiagnosticTest(
        "ecg", "Electrocardiogram (12-lead)", "Cardiology", ("93000",), "", "", "", None, None, "Normal sinus rhythm",
        ("R07", "I48", "I10", "R00", "R55", "I25", "R94.31", "I50"), ("ecg", "ekg", "electrocardiogram"),
        ("chest pain", "palpitations", "syncope", "atrial fibrillation", "arrhythmia"), 0, False, True,
    ),
    DiagnosticTest(
        "cxr", "Chest X-ray, 2 views", "Imaging", ("71046",), "", "", "", None, None, "No acute cardiopulmonary process",
        ("J18", "R05", "R06", "J44", "J45", "R07", "R50"), ("chest x-ray", "chest x ray", "chest radiograph", "cxr"),
        ("pneumonia", "persistent cough", "shortness of breath", "dyspnea"), 0,
    ),
    DiagnosticTest(
        "spirometry", "Spirometry with bronchodilator", "Pulmonary", ("94060",), "", "", "", None, None, "FEV1/FVC >= 0.70",
        ("J44", "J45", "R06", "R05"), ("spirometry", "pulmonary function test", "pft"), ("copd", "asthma", "wheezing"), 365, True,
    ),
    DiagnosticTest(
        "echo", "Echocardiogram (transthoracic)", "Cardiology", ("93306",), "", "", "", None, None, "LVEF 55-70%",
        ("I50", "R01", "I35", "I34", "R06.0", "I42", "I11.0"), ("echocardiogram", "echo", "ejection fraction"),
        ("heart failure", "heart murmur", "cardiomyopathy"), 0, False, False, True,
    ),
    DiagnosticTest(
        "ct_chest", "CT chest without contrast", "Imaging", ("71250",), "", "", "", None, None, "No acute findings",
        ("R91", "J84", "R05", "R04.2", "J18"), ("ct chest", "chest ct", "ct of the chest", "ct scan of the chest"),
        ("lung nodule", "pulmonary nodule"), 0, False, False, True,
    ),
    DiagnosticTest(
        "ldct", "Low-dose CT lung cancer screening", "Screening", ("71271",), "", "", "", None, None, "Lung-RADS 1",
        ("Z87.891", "F17.21", "Z12.2", "Z72.0"), ("low-dose ct", "low dose ct", "lung cancer screening", "ldct"),
        ("heavy smoker", "pack-year"), 365,
    ),
    DiagnosticTest(
        "ct_head", "CT head without contrast", "Imaging", ("70450",), "", "", "", None, None, "No acute intracranial process",
        ("R51", "S06", "G45", "R42", "R55", "I63"), ("ct head", "head ct", "ct of the head", "ct scan of the head"),
        ("head injury", "stroke"), 0, False, False, True,
    ),
    DiagnosticTest(
        "mri_brain", "MRI brain without contrast", "Imaging", ("70551",), "", "", "", None, None, "No acute intracranial abnormality",
        ("G43", "R51", "G45", "R42", "G35", "G40"), ("mri brain", "brain mri", "mri of the brain", "neuroimaging"),
        ("migraine with aura", "seizure", "multiple sclerosis"), 0, False, False, True,
    ),
    DiagnosticTest(
        "xr_knee", "Knee X-ray, 3 views", "Imaging", ("73562",), "", "", "", None, None, "Mild degenerative change",
        ("M17", "M25.56", "S83", "S80"), ("knee x-ray", "knee x ray", "x-ray of the knee", "knee radiograph"),
        ("knee osteoarthritis", "knee pain", "knee injury"), 0,
    ),
    DiagnosticTest(
        "mri_knee", "MRI knee without contrast", "Imaging", ("73721",), "", "", "", None, None, "No meniscal tear",
        ("M23", "S83", "M25.56", "M17"), ("mri knee", "knee mri", "mri of the knee"), ("meniscal tear", "ligament tear"),
        0, False, False, True,
    ),
    DiagnosticTest(
        "us_abd", "Ultrasound abdomen, complete", "Imaging", ("76700",), "", "", "", None, None, "Unremarkable",
        ("R10", "K80", "R74", "K76"), ("abdominal ultrasound", "ultrasound of the abdomen", "abdomen ultrasound"),
        ("gallstones", "right upper quadrant pain", "fatty liver"), 0,
    ),
    DiagnosticTest(
        "mammo", "Screening mammogram", "Screening", ("77067",), "", "", "", None, None, "BI-RADS 1",
        ("Z12.31", "Z80.3"), ("mammogram", "mammography", "breast cancer screening"), (), 365,
    ),
    DiagnosticTest(
        "stress", "Exercise stress test", "Cardiology", ("93015",), "", "", "", None, None, "No inducible ischemia",
        ("R07", "I20", "I25"), ("stress test", "treadmill test", "exercise test"), ("angina", "exertional chest pain"), 0,
    ),
    DiagnosticTest(
        "mpi", "Nuclear myocardial perfusion study", "Cardiology", ("78452",), "", "", "", None, None, "Normal perfusion",
        ("R07", "I20", "I25"), ("nuclear stress", "myocardial perfusion", "spect", "pharmacologic stress"), (), 0,
        False, False, True,
    ),
    DiagnosticTest(
        "psg", "Sleep study (polysomnography)", "Pulmonary", ("95810",), "", "", "", None, None, "AHI < 5",
        ("G47.3", "R06.83", "G47.1"), ("sleep study", "polysomnography", "psg"), ("sleep apnea", "snoring", "osa"), 0,
        False, False, True,
    ),
    DiagnosticTest(
        "retinal", "Diabetic retinal imaging", "Screening", ("92250",), "", "", "", None, None, "No retinopathy",
        ("E11", "E10", "E13"), ("retinal exam", "eye exam", "retinal imaging", "fundus photo", "dilated eye"),
        ("diabetic retinopathy",), 365, True,
    ),
    DiagnosticTest(
        "phq9", "Depression screening (PHQ-9)", "Screening", ("96127",), "44249-1", "", "score", None, 4.0, "0-4 minimal",
        ("F32", "F33", "F41", "Z13.31", "Z13.39"), ("phq-9", "phq9", "depression screening", "gad-7"),
        ("depression", "anxiety", "low mood"), 0, False, True,
    ),
)
BY_KEY = {test.key: test for test in CATALOG}
_NEGATION = re.compile(r"\b(no|not|denies|denied|without|negative for|ruled out|rules out|never)\b[^.;\n]{0,40}$")


def _pattern(phrases: tuple[str, ...]) -> re.Pattern | None:
    if not phrases:
        return None
    alternatives = "|".join(re.escape(phrase) for phrase in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])")


_KEYWORD_PATTERNS = {test.key: _pattern(test.keywords) for test in CATALOG}
_CONDITION_PATTERNS = {test.key: _pattern(test.conditions) for test in CATALOG}


def _mention(pattern: re.Pattern | None, text: str) -> str | None:
    """First phrase the pattern finds that is not negated in the same clause ("denies chest pain")."""
    if pattern is None or not text:
        return None
    for match in pattern.finditer(text):
        if not _NEGATION.search(text[max(0, match.start() - 60):match.start()]):
            return match.group(0)
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d %b %Y %H:%M", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _rows(chart: dict[str, Any] | None, sheet: str) -> list[dict[str, Any]]:
    rows = ((chart or {}).get("records") or {}).get(sheet) or []
    return [row for row in rows if isinstance(row, dict)]


def _section(note: str, *headings: str) -> str:
    """Text under the given SOAP headings (Assessment, Plan), or the whole note if it has no headings."""
    text = str(note or "")
    names = "|".join(re.escape(heading) for heading in headings)
    match = re.search(rf"(?ims)^\s*#*\s*(?:{names})\b[^\n]*\n(.*?)(?=^\s*#*\s*(?:subjective|objective|assessment|plan)\b|\Z)", text)
    return match.group(1) if match else text


def encounter_diagnoses(record: dict[str, Any], chart: dict[str, Any] | None) -> list[dict[str, str]]:
    """Coded encounter diagnoses first, then the active problem list; one row per ICD-10-CM code."""
    seen, rows = set(), []
    for item in ((record or {}).get("code_results") or {}).get("icd10", []) or []:
        code = str((item or {}).get("ICD-10 Code") or "").strip().upper()
        if code and code not in seen:
            seen.add(code)
            rows.append({"code": code, "description": str(item.get("Matched Disease/Injury") or ""), "source": "Encounter"})
    for problem in _rows(chart, "Problems"):
        code = str(problem.get("ICD_10_CM") or "").strip().upper()
        status = str(problem.get("Clinical_Status") or "Active").strip().lower()
        if code and code not in seen and status in {"active", "recurrence", "relapse", ""}:
            seen.add(code)
            rows.append({"code": code, "description": str(problem.get("Problem_Name") or ""), "source": "Problem list"})
    return rows


def last_results(chart: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Most recent result per LOINC code from the chart's Labs sheet."""
    latest: dict[str, dict[str, Any]] = {}
    for row in _rows(chart, "Labs"):
        loinc = str(row.get("LOINC_Code") or "").strip()
        when = _parse_date(row.get("Result_Date"))
        if not loinc or when is None:
            continue
        if loinc not in latest or when > latest[loinc]["date"]:
            latest[loinc] = {"date": when, "result": row.get("Result"), "unit": row.get("Unit") or "", "flag": row.get("Flag") or ""}
    return latest


def due_status(test: DiagnosticTest, latest: dict[str, dict[str, Any]], today: date) -> dict[str, Any]:
    candidates = [latest[code] for code in test.history_loincs if code in latest]
    previous = max(candidates, key=lambda item: item["date"]) if candidates else None
    if previous is None:
        return {"due": True, "label": "Due · no prior result on file", "last": None, "next_due": None}
    last_text = f"{previous['result']} {previous['unit']}".strip()
    if test.interval_days:
        next_due = previous["date"] + timedelta(days=test.interval_days)
        if next_due > today:
            return {
                "due": False,
                "label": f"Not due · last {last_text} on {previous['date']:%d %b %Y}; repeat from {next_due:%d %b %Y}",
                "last": previous,
                "next_due": next_due,
            }
    return {"due": True, "label": f"Due · last {last_text} on {previous['date']:%d %b %Y}", "last": previous, "next_due": None}


def linked_diagnoses(test: DiagnosticTest, diagnoses: list[dict[str, str]]) -> list[str]:
    return [row["code"] for row in diagnoses if any(row["code"].startswith(prefix) for prefix in test.indications)]


def active_orders(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [order for order in (record or {}).get("diagnostic_orders") or [] if order.get("status") in ACTIVE_STATUSES]


def suggest_tests(record: dict[str, Any], chart: dict[str, Any] | None, today: date | None = None) -> list[dict[str, Any]]:
    """Tests the encounter implies, strongest evidence first.

    Evidence: the CDS recommendation names the test (3), the assessment/plan names the test or a
    condition that calls for it (2), a coded encounter diagnosis supports it (2), the active problem
    list supports a chronic-monitoring test (1). Tests already ordered on this encounter are skipped.
    """
    today = today or date.today()
    note = str((record or {}).get("summary") or "")
    plan_text = (_section(note, "assessment", "plan") + "\n" + str((record or {}).get("patient_summary") or "")).lower()
    cds_text = str((record or {}).get("recommendations") or "").lower()
    diagnoses = encounter_diagnoses(record, chart)
    latest = last_results(chart)
    already = {order.get("test_key") for order in active_orders(record)}
    suggestions = []
    for test in CATALOG:
        if test.key in already:
            continue
        reasons, strength = [], 0
        if phrase := _mention(_KEYWORD_PATTERNS[test.key], cds_text):
            reasons.append(f"CDS recommendation mentions “{phrase}”")
            strength = max(strength, 3)
        if phrase := _mention(_KEYWORD_PATTERNS[test.key], plan_text):
            reasons.append(f"Plan mentions “{phrase}”")
            strength = max(strength, 2)
        elif phrase := _mention(_CONDITION_PATTERNS[test.key], plan_text):
            reasons.append(f"Assessment: {phrase}")
            strength = max(strength, 2)
        linked = linked_diagnoses(test, diagnoses)
        encounter_dx = [row for row in diagnoses if row["code"] in linked and row["source"] == "Encounter"]
        problem_dx = [row for row in diagnoses if row["code"] in linked and row["source"] == "Problem list"]
        if encounter_dx:
            reasons.append("Diagnosis " + ", ".join(f"{row['description'] or row['code']}" for row in encounter_dx[:2]))
            # A diagnosis alone pre-selects only standard-of-care monitoring (e.g. A1c for diabetes).
            strength = max(strength, 2 if test.chronic_monitoring else 1)
        if problem_dx and test.chronic_monitoring:
            reasons.append("Problem list: " + ", ".join(row["description"] or row["code"] for row in problem_dx[:2]))
            strength = max(strength, 1)
        if not strength:
            continue
        due = due_status(test, latest, today)
        condition = _mention(_CONDITION_PATTERNS[test.key], plan_text)
        if encounter_dx:
            note_reason = f"for {(encounter_dx[0]['description'] or encounter_dx[0]['code']).lower()}"
        elif condition:
            note_reason = f"for {condition}"
        elif any(reason.startswith("CDS") for reason in reasons):
            note_reason = "recommended by clinical decision support"
        elif problem_dx:
            note_reason = f"monitoring {(problem_dx[0]['description'] or problem_dx[0]['code']).lower()}"
        else:
            note_reason = ""
        suggestions.append({
            "test_key": test.key,
            "name": test.name,
            "category": test.category,
            "reasons": reasons,
            "strength": strength,
            "due": due,
            "linked_dx": linked,
            "preselect": strength >= 2 and due["due"],
            "prior_auth_candidate": test.prior_auth,
            "note_reason": note_reason,
        })
    suggestions.sort(key=lambda item: (-item["strength"], not item["due"]["due"], item["name"]))
    return suggestions


def new_order(
    test_key: str,
    record: dict[str, Any],
    *,
    reasons: list[str] | None = None,
    linked_dx: list[str] | None = None,
    ordered_by: str = "",
    priority: str = "Routine",
    source: str = "CDS",
    now: datetime | None = None,
) -> dict[str, Any]:
    test = BY_KEY[test_key]
    now = now or datetime.now()
    return {
        "order_id": "",  # assigned by the EHR (ORD########) when the order is saved
        "test_key": test.key,
        "name": test.name,
        "category": test.category,
        "cpts": list(test.cpts),
        "loinc": test.loinc,
        "specimen": test.specimen,
        "status": ORDERED,
        "priority": priority if priority in {"Routine", "STAT"} else "Routine",
        "source": source,
        "reasons": list(reasons or []),
        "linked_dx": list(linked_dx or []),
        "ordered_by": ordered_by,
        "ordered_at": now.strftime("%Y-%m-%d %H:%M"),
        "encounter_id": str((record or {}).get("ehr_encounter_id") or (record or {}).get("id") or ""),
        "prior_auth": {"required": False},
        "result": None,
        "performed_at": None,
    }


def result_flag(test: DiagnosticTest, value: Any) -> str:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        text = str(value or "").strip().lower()
        if not text:
            return ""
        normal_words = ("negative", "normal", "nonreactive", "non-reactive", "unremarkable", "no acute", "within normal")
        return "Normal" if any(word in text for word in normal_words) else "Abnormal"
    if test.high is not None and number > test.high:
        return "High"
    if test.low is not None and number < test.low:
        return "Low"
    return "Normal"


def mark_performed(order: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    if order.get("status") == ORDERED:
        order["status"] = PERFORMED
        order["performed_at"] = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    return order


def record_result(order: dict[str, Any], value: Any, now: datetime | None = None) -> dict[str, Any]:
    """Store a result; resulting a test also means it was performed (the billable event)."""
    test = BY_KEY[order["test_key"]]
    text = str(value or "").strip()
    if not text:
        raise ValueError("Enter a result value.")
    now = now or datetime.now()
    if order.get("status") == CANCELLED:
        raise ValueError("A cancelled order cannot be resulted.")
    order["performed_at"] = order.get("performed_at") or now.strftime("%Y-%m-%d %H:%M")
    order["status"] = RESULTED
    order["result"] = {
        "value": text,
        "unit": test.unit,
        "reference": test.reference,
        "flag": result_flag(test, text),
        "resulted_at": now.strftime("%Y-%m-%d %H:%M"),
    }
    return order


def cancel_order(order: dict[str, Any], reason: str = "") -> dict[str, Any]:
    if order.get("status") in BILLABLE_STATUSES:
        raise ValueError("A performed test cannot be cancelled; correct it with a coder instead.")
    order["status"] = CANCELLED
    order["cancel_reason"] = reason
    return order


def plan_line(order: dict[str, Any], suggestion: dict[str, Any] | None = None) -> str:
    """The sentence added to the Plan: test name and the clinical reason, never a billing code."""
    reason = str((suggestion or {}).get("note_reason") or "")
    due = (suggestion or {}).get("due") or {}
    last = due.get("last")
    history = f" (last {last['result']}{'' if str(last['unit']).startswith('%') else ' '}{last['unit']} on {last['date']:%d %b %Y})" if last else ""
    priority = " (STAT)" if order.get("priority") == "STAT" else ""
    return f"Order: {order['name']}{priority}" + (f" — {reason}" if reason else "") + history


def ehr_order_row(order: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Orders_Procedures row: the EHR (Snowflake EHR.SHEETS) copy of the order, linked by MRN and encounter."""
    test = BY_KEY[order["test_key"]]
    pa = order.get("prior_auth") or {}
    result = order.get("result") or {}
    return {
        "Order_ID": order.get("order_id") or "",
        "Patient_ID": str((record or {}).get("ehr_patient_id") or ""),
        "MRN": str((record or {}).get("mrn") or ""),
        "Encounter_ID": order.get("encounter_id") or str((record or {}).get("ehr_encounter_id") or ""),
        "Order_Date": str(order.get("ordered_at") or "")[:10],
        "Order_Type": test.category,
        "CPT_HCPCS": "+".join(test.cpts),
        "LOINC_Code": test.loinc,
        "Order_Description": test.name,
        "Status": order.get("status"),
        "Ordering_Provider": (record or {}).get("doctor") or order.get("ordered_by") or "",
        "Facility": "NuuCare Demo",
        "Priority": order.get("priority") or "Routine",
        "Linked_Diagnosis": ";".join(order.get("linked_dx") or []),
        "Order_Source": order.get("source") or "",
        "Prior_Auth_Status": pa.get("status") or ("Not required" if not pa.get("required") else "Required"),
        "Auth_Number": pa.get("auth_number") or "",
        "Performed_Date": str(order.get("performed_at") or "")[:10],
        "Result_Value": result.get("value") or "",
        "Result_Flag": result.get("flag") or "",
    }


def ehr_lab_row(order: dict[str, Any], record: dict[str, Any]) -> dict[str, Any] | None:
    """Labs row for a resulted laboratory test (so the next visit's due-check sees it)."""
    test = BY_KEY[order["test_key"]]
    result = order.get("result") or {}
    if order.get("status") != RESULTED or not test.loinc:
        return None
    return {
        "Lab_ID": order.get("lab_id") or "",
        "Patient_ID": str((record or {}).get("ehr_patient_id") or ""),
        "Result_Date": str(result.get("resulted_at") or "")[:10],
        "LOINC_Code": test.loinc,
        "Test_Name": test.name,
        "Result": result.get("value"),
        "Unit": test.unit,
        "Reference_Range": test.reference,
        "Flag": result.get("flag") or "",
        "Status": "Final",
        "Performing_Lab": IN_HOUSE_LAB,
        "Ordering_Provider": (record or {}).get("doctor") or "",
        "Encounter_ID": order.get("encounter_id") or "",
        "Order_ID": order.get("order_id") or "",
    }


def billing_lines(record: dict[str, Any]) -> list[dict[str, Any]]:
    """One line per CPT of every active order, with its billing state.

    Performed/resulted tests are billable (the order and its result are the performed evidence).
    Tests still only ordered are held, and a test whose payer authorization is not approved is held
    so the claim is not denied for missing authorization (CARC 197). Blood drawn in-house adds one
    venipuncture (36415) per encounter.
    """
    lines = []
    blood_dx: list[str] = []
    blood_drawn = False
    for order in active_orders(record):
        test = BY_KEY.get(order.get("test_key"))
        if test is None:
            continue
        pa = order.get("prior_auth") or {}
        status = order.get("status")
        result = order.get("result") or {}
        if status in BILLABLE_STATUSES and pa.get("required") and pa.get("status") != "Approved":
            state, reason = "HELD", f"Prior authorization {str(pa.get('status') or 'missing').lower()} — CARC 197 denial risk"
        elif status in BILLABLE_STATUSES:
            state = "BILLABLE"
            reason = f"{test.performed_label} {str(order.get('performed_at') or '')[:10]}".strip()
            if result:
                reason += f" · result {result.get('value')} {result.get('unit') or ''}".rstrip() + (
                    f" ({result['flag']})" if result.get("flag") and result["flag"] != "Normal" else "")
        else:
            state, reason = "PENDING", "Ordered — not yet performed"
        for cpt in test.cpts:
            lines.append({
                "code": cpt,
                "service": test.name,
                "order_id": order.get("order_id") or "",
                "test_key": test.key,
                "state": state,
                "reason": reason,
                "linked_dx": list(order.get("linked_dx") or []),
                "auth_number": pa.get("auth_number") or "",
                "prior_auth_required": bool(pa.get("required")),
            })
        if state == "BILLABLE" and test.specimen == "Blood":
            blood_drawn = True
            blood_dx.extend(order.get("linked_dx") or [])
    if blood_drawn:
        lines.append({
            "code": VENIPUNCTURE_CPT,
            "service": "Routine venipuncture (specimen collection)",
            "order_id": "",
            "test_key": "",
            "state": "BILLABLE",
            "reason": "Blood specimen collected in-house",
            "linked_dx": list(dict.fromkeys(blood_dx)),
            "auth_number": "",
            "prior_auth_required": False,
        })
    return lines
