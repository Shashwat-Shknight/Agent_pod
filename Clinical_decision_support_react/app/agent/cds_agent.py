import re

from app.llm_client import call_llm
from app.agent.context import EncounterContext

MAX_PROBLEMS = 5
PASSAGES_PER_PROBLEM = 2

# Wording that steers retrieval toward the management sections of Harrison's for common problems.
_QUERY_EXPANSIONS = (
    (r"neuropath", "diabetic peripheral polyneuropathy foot examination monofilament treatment"),
    (r"diabet|hyperglyc|\bdm\b|\bt2dm\b", "type 2 diabetes mellitus glycemic control metformin HbA1c treatment"),
    (r"hypertens|blood pressure", "hypertension treatment blood pressure goal antihypertensive ACE inhibitor"),
    (r"lipid|cholesterol|statin", "statin therapy LDL cholesterol cardiovascular risk"),
    (r"kidney|renal|ckd|egfr|albumin", "chronic kidney disease albuminuria eGFR"),
    (r"heart failure", "heart failure treatment"),
    (r"anemia", "anemia evaluation treatment"),
)


_NEXT_HEADING = r"Plan|Objective|Current Medications|Past Medical History|History of Present Illness|Chief Complaint|Subjective"


def _section(text: str, heading: str) -> str:
    match = re.search(rf"(?is)\b{heading}\s*:\s*(.*?)(?:\n\s*(?:{_NEXT_HEADING})\s*:|\Z)", text or "")
    return match.group(1).strip() if match else ""


def _split(text: str, pattern: str) -> list[str]:
    parts = [re.sub(r"^[\s\-*•\d.)]+", "", part).strip(" .,:") for part in re.split(pattern, text)]
    return [part for part in parts if len(part) >= 4]


def encounter_problems(encounter: EncounterContext) -> list[str]:
    """Diagnoses from the scribe's assessment, one per line/number/semicolon (falls back to the summary)."""
    assessment = (encounter.assessment or "").strip() or _section(encounter.patient_summary, "Assessment")
    if not assessment:
        return []
    parts = _split(assessment, r"\n+|;|(?<![\d.])\b\d+[.)]\s+")
    if len(parts) == 1 and "," in parts[0]:  # one sentence listing several diagnoses
        parts = _split(parts[0], r",\s*(?:and\s+)?|\s+and\s+")
    problems = []
    for part in parts:
        if part.lower() not in {p.lower() for p in problems}:
            problems.append(part)
    return problems[:MAX_PROBLEMS]


def problem_query(problem: str) -> str:
    lowered = problem.lower()
    for pattern, expansion in _QUERY_EXPANSIONS:
        if re.search(pattern, lowered):
            return f"{problem} {expansion}"
    return problem


def _source_label(metadata: dict) -> str:
    source = str(metadata.get("source") or "knowledge base")
    name = "Harrison's Principles of Internal Medicine" if "harrison" in source.lower() else source
    return f"{name}, passage {metadata.get('chunk', metadata.get('block', '?'))}"


def grounded_guideline_context(encounter: EncounterContext) -> str:
    """Retrieve knowledge-base passages per diagnosis so each problem gets its own evidence."""
    from app.core.config import MAX_CONTEXT_CHARS
    from app.rag.retriever import retrieve_chunks, retrieve_documents

    problems = encounter_problems(encounter)
    if not problems:
        return retrieve_documents(encounter.patient_summary, top_k=5)
    entries, seen, total = [], set(), 0
    for problem in problems:
        for chunk in retrieve_chunks(problem_query(problem), results=PASSAGES_PER_PROBLEM):
            key = (chunk["metadata"].get("source"), chunk["metadata"].get("chunk"), chunk["text"][:80])
            if key in seen:
                continue
            seen.add(key)
            entry = f"Source: {_source_label(chunk['metadata'])} · Topic: {problem}\n{chunk['text']}"
            if total + len(entry) > MAX_CONTEXT_CHARS:
                break
            entries.append(entry)
            total += len(entry)
    return "\n\n".join(entries) or "No highly relevant context found in the current knowledge base."


class ClinicalDecisionSupportAgent:
    name = "clinical_decision_support"

    @staticmethod
    def _fallback_recommendation(patient_summary: str) -> str:
        return (
            "Recommendation\n"
            "Review the documented symptoms, relevant history, medications, and vital signs; "
            "confirm the assessment and arrange appropriate follow-up based on clinical judgment.\n\n"
            "Reasoning\n"
            f"The available encounter information was: {patient_summary.strip() or 'limited'}.\n\n"
            "Safety Note\n"
            "This is educational decision support and does not replace evaluation by a qualified clinician."
        )

    def analyze(self, patient_summary):
        encounter = EncounterContext(patient_summary=patient_summary)
        self.analyze_context(encounter)
        return encounter.recommendations

    def analyze_context(self, encounter: EncounterContext) -> EncounterContext:
        guideline_context = grounded_guideline_context(encounter)
        encounter.retrieved_guidelines = guideline_context
        problems = encounter_problems(encounter)
        problem_list = "\n".join(f"- {problem}" for problem in problems) or "- (derive from the summary)"
        prompt = (
            "You are a Clinical Decision Support Assistant for the treating clinician.\n"
            "Use ONLY the patient summary and the retrieved Harrison's Principles of Internal Medicine passages below.\n"
            "Give one or two actionable recommendations for each documented problem, grounded in the passages.\n"
            "Account for the current medications and allergies: titrate or continue a drug the patient already takes "
            "instead of recommending to start it, and never suggest an agent the patient is allergic to.\n"
            "Do not assign or mention billing codes (ICD-10, CPT, HCC); coding is done by the coding team.\n\n"
            "Answer in exactly this format:\n"
            "Recommendation\n"
            "1. **<short action title>**: <what to do and why, citing (Harrison's, passage N)>\n"
            "2. ...\n\n"
            "Reasoning\n<how the documented findings and the passages support the recommendations>\n\n"
            "Safety Note\n<allergies, interactions or red flags to watch; state that this is decision support only>\n\n"
            f"Documented problems:\n{problem_list}\n\n"
            f"Patient Summary:\n{encounter.patient_summary}\n\n"
            f"Retrieved guideline context:\n{guideline_context}\n"
        )
        try:
            recommendation = call_llm(
                prompt=prompt,
                system_prompt="You are a Clinical Decision Support Assistant. Ground every recommendation in the supplied passages.",
            )
        except Exception:
            recommendation = ""
        encounter.recommendations = str(recommendation).strip() or self._fallback_recommendation(
            encounter.patient_summary
        )
        return encounter

    def generate(self, patient_summary):
        return self.analyze(patient_summary)
