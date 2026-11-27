"""Versioned rule packs: clinical, coding and payer policy kept as data, not code.

Every agent reads its policy from a JSON pack in ``app/rules/packs/`` (or from a directory named by
the ``RULES_DIR`` environment variable, so a site can override a pack without a code change). A pack
carries its own metadata - version, effective date, owner and sources - and is re-read when the
file changes, so an informatics or revenue-integrity analyst can update a threshold, a payer
contract or a test catalog entry and see it on the next screen refresh.

What deliberately stays in code: algorithms (negation detection, adjudication order), standards that
do not vary by site (X12 segment layout, CPT E/M code ranges), and system design (ID formats).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

PACK_DIR = Path(__file__).resolve().parent / "packs"
_REQUIRED_META = ("pack", "version", "effective", "owner")
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()


class RulePackError(RuntimeError):
    pass


def pack_path(name: str) -> Path:
    override = os.environ.get("RULES_DIR", "").strip()
    if override:
        candidate = Path(override) / f"{name}.json"
        if candidate.is_file():
            return candidate
    return PACK_DIR / f"{name}.json"


def load_pack(name: str) -> dict[str, Any]:
    """The named pack, validated and cached until its file changes."""
    path = pack_path(name)
    try:
        mtime = path.stat().st_mtime
    except OSError as error:
        raise RulePackError(f"Rule pack '{name}' not found at {path}") from error
    with _LOCK:
        cached = _CACHE.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            pack = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RulePackError(f"Rule pack '{name}' is not valid JSON: {error}") from error
        missing = [field for field in _REQUIRED_META if not pack.get(field)]
        if missing or pack.get("pack") != name:
            raise RulePackError(f"Rule pack '{name}' is missing metadata {missing or ['pack name']}")
        _CACHE[str(path)] = (mtime, pack)
        return pack


def section(name: str, key: str, default: Any = None) -> Any:
    return load_pack(name).get(key, default)


def plan_year_values(name: str, year: int) -> dict[str, Any]:
    """Values for a plan year; falls back to the latest year the pack knows (and says so)."""
    years = load_pack(name).get("plan_years") or {}
    if str(year) in years:
        return {**years[str(year)], "plan_year": year, "fallback": False}
    known = sorted(int(key) for key in years)
    if not known:
        return {"plan_year": year, "fallback": True}
    nearest = max((value for value in known if value <= year), default=known[0])
    return {**years[str(nearest)], "plan_year": nearest, "fallback": True}


def describe() -> list[dict[str, Any]]:
    """Metadata of every pack in use (for the admin screen and audits)."""
    names = sorted({path.stem for path in PACK_DIR.glob("*.json")})
    rows = []
    for name in names:
        try:
            pack = load_pack(name)
            rows.append({"pack": name, "version": pack["version"], "effective": pack["effective"], "owner": pack["owner"],
                         "source_file": str(pack_path(name)), "sources": "; ".join(pack.get("sources", []))})
        except RulePackError as error:
            rows.append({"pack": name, "version": "INVALID", "effective": "", "owner": "", "source_file": "", "sources": str(error)})
    return rows
