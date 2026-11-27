"""Payer connectivity behind one interface: eligibility (270/271), prior auth (278), claim acknowledgement
(837 -> 277CA) and remittance (835).

The demo runs on ``SimulatedPayerGateway``: deterministic, explainable responses built from the rule packs,
the chargemaster contracts and the member's benefits. A production deployment implements the same four
methods against a clearinghouse (Availity, Change Healthcare / Optum, Waystar...) and selects it with
``PAYER_GATEWAY=<name>`` in .env - no screen or workflow code changes.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any, Protocol

from frontend import prior_auth as pa
from frontend import rcm


class PayerGateway(Protocol):
    name: str

    def eligibility(self, coverage: dict[str, Any] | None, service_date: date, **accumulators: Any) -> dict[str, Any]: ...

    def prior_authorization(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def acknowledge(self, claim: dict[str, Any]) -> tuple[bool, list[str]]: ...

    def remittance(self, lines: list[rcm.ClaimLine], benefits: dict[str, Any], primary_dx: str = "") -> dict[str, Any]: ...


class SimulatedPayerGateway:
    """Rule-driven stand-in for a clearinghouse. Every response says it is simulated."""

    name = "simulated"
    label = "Simulated payer (rule packs + chargemaster contracts)"

    def eligibility(self, coverage, service_date, prior_liability=0.0, accumulators=None):
        return rcm.plan_benefits(coverage, service_date, prior_liability=prior_liability, accumulators=accumulators)

    def prior_authorization(self, request):
        return pa.submit(request)

    def acknowledge(self, claim):
        """277CA front-end edits: a claim without subscriber, payer or diagnosis is rejected."""
        info = claim.get("Patient Information") or {}
        problems = [label for label, value in (("subscriber/member ID", info.get("Member ID")), ("payer", info.get("Payer")),
                                               ("diagnosis code", claim.get("ICD-10 Codes"))) if not value]
        return not problems, problems

    def remittance(self, lines, benefits, primary_dx=""):
        return rcm.adjudicate(lines, benefits, primary_dx=primary_dx)


_GATEWAYS = {"simulated": SimulatedPayerGateway}


def get_gateway() -> SimulatedPayerGateway:
    name = os.getenv("PAYER_GATEWAY", "simulated").strip().lower() or "simulated"
    if name not in _GATEWAYS:
        raise RuntimeError(
            f"PAYER_GATEWAY={name!r} has no connector in this build. Available: {', '.join(sorted(_GATEWAYS))}. "
            "Implement the PayerGateway methods for your clearinghouse and register it in frontend/payer_gateway.py."
        )
    return _GATEWAYS[name]()


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
