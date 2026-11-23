"""Outreach compliance gate: consent, channel, language, quiet hours, survey integrity, minimum-necessary PHI."""

from datetime import datetime

from app.domain.vbc.catalog import QUIET_HOURS

SURVEY_TERMS = ("survey", "cahps", " hos ", "rate us", "star rating", "questionnaire", "how would you rate")
PHI_TERMS = ("diabet", "hypertens", "hiv", "depress", "a1c", "kidney", "dialysis", "pregnan", "mastectomy", "cancer diagnosis")


def outreach_checks(person, text: str, channel: str, now: datetime, has_template: bool) -> list[tuple[str, bool, str]]:
    quiet_start, quiet_end = QUIET_HOURS
    in_quiet = now.hour >= quiet_start or now.hour < quiet_end
    lowered = f" {text.lower()} "
    interpreter = not has_template and channel == "Phone"
    if channel == "Phone":
        consent_note = "Phone consent active" if person.consent_phone else "Phone consent withdrawn"
    else:
        consent_note = "SMS / portal consent active" if person.consent_sms else "No SMS consent"
    language_note = (f"{person.language} · approved" if has_template
                     else f"{person.language} · interpreter call script" if interpreter else f"No {person.language} SMS template")
    return [
        ("Consent on file", person.consent_phone if channel == "Phone" else person.consent_sms, consent_note),
        ("Channel allowed", channel == "Phone" or person.consent_sms, f"{channel}"),
        ("Language template approved", has_template or interpreter, language_note),
        ("Quiet hours respected", not in_quiet, f"Now {now:%H:%M}; quiet {quiet_start}:00–0{quiet_end}:00"),
        ("No survey reference", not any(term in lowered for term in SURVEY_TERMS), "Survey-integrity scan"),
        ("Minimum necessary PHI", not any(term in lowered for term in PHI_TERMS), "No diagnosis in message"),
    ]


_checks = outreach_checks
