"""CMS-HCC risk-adjustment mapping with hierarchies, driven by data.

Sources, in priority order:
1. ``data/rules/hcc_v28_mappings.csv`` - the official CMS ICD-10-CM -> CMS-HCC mapping, created by
   ``python -m app.scripts.import_hcc_mappings <CMS mappings file>`` (about 8,000 payment codes).
2. The ``hcc_v28`` rule pack - a seed of common chronic conditions, with category labels and the
   V28 hierarchies (a more severe HCC in a family drops the less severe ones).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable

from app.rules import packs

OFFICIAL_MAPPING = Path(__file__).resolve().parents[1] / "data" / "rules" / "hcc_v28_mappings.csv"
_CACHE: dict[str, Any] = {"key": None, "mapping": {}}


def _norm(code: Any) -> str:
    return str(code or "").replace(".", "").strip().upper()


def _official() -> tuple[float | None, dict[str, str]]:
    try:
        mtime = OFFICIAL_MAPPING.stat().st_mtime
    except OSError:
        return None, {}
    with OFFICIAL_MAPPING.open(encoding="utf-8", newline="") as handle:
        return mtime, {_norm(row["code"]): str(row["hcc"]).strip() for row in csv.DictReader(handle) if row.get("hcc")}


def mapping() -> dict[str, str]:
    """ICD-10-CM (no dots) -> V28 HCC number, official file over the seed."""
    pack = packs.load_pack("hcc_v28")
    official_mtime = OFFICIAL_MAPPING.stat().st_mtime if OFFICIAL_MAPPING.is_file() else None
    key = (id(pack), official_mtime)
    if _CACHE["key"] != key:
        merged = {_norm(code): str(hcc) for code, hcc in (pack.get("mappings") or {}).items()}
        merged.update(_official()[1])
        _CACHE.update(key=key, mapping=merged)
    return _CACHE["mapping"]


def source() -> str:
    pack = packs.load_pack("hcc_v28")
    if OFFICIAL_MAPPING.is_file():
        return f"{pack['model']} official CMS mapping ({len(mapping())} codes)"
    return f"{pack['model']} seed subset ({len(pack.get('mappings') or {})} codes) · import the CMS file for full coverage"


def hcc_number(icd_code: Any) -> str:
    return mapping().get(_norm(icd_code), "")


def hcc_for(icd_code: Any) -> str:
    """Display form ("HCC 38") or "" when the code carries no payment HCC."""
    number = hcc_number(icd_code)
    return f"HCC {number}" if number else ""


def label(hcc: Any) -> str:
    number = str(hcc or "").replace("HCC", "").strip()
    return (packs.load_pack("hcc_v28").get("labels") or {}).get(number, "")


def apply_hierarchy(hccs: Iterable[Any]) -> list[str]:
    """Drop HCCs trumped by a more severe HCC in the same family (V28 hierarchies)."""
    numbers = {str(item).replace("HCC", "").strip() for item in hccs if str(item or "").strip()}
    hierarchy = {key: set(value) for key, value in (packs.load_pack("hcc_v28").get("hierarchies") or {}).items()
                 if not key.startswith("_")}
    dropped = {lower for number in numbers for lower in hierarchy.get(number, set())}
    kept = sorted(numbers - dropped, key=lambda value: int(value) if value.isdigit() else 0)
    return [f"HCC {number}" for number in kept]
