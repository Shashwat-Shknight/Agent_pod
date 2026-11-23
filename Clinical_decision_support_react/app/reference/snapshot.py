"""Bundled offline snapshot of the official code sets (data/reference/).

Used only when the live sources (CDC, NLM) cannot be reached - e.g. a corporate network that blocks
them - so a fresh clone still gets a complete ICD-10-CM and HCPCS code set. Live sync replaces it
automatically as soon as the sources are reachable.

Refresh the snapshot (maintainers, with internet access):
    .venv\\Scripts\\python -m app.reference.snapshot export
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import zipfile
from datetime import date
from pathlib import Path

from app.reference import sources

SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "data" / "reference"
MANIFEST = SNAPSHOT_DIR / "manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else {}


def available(code_system: str) -> bool:
    entry = _manifest().get(code_system)
    return bool(entry) and (SNAPSHOT_DIR / entry["file"]).is_file()


def load(code_system: str) -> sources.Release:
    entry = _manifest().get(code_system)
    if not entry:
        raise LookupError(f"No bundled snapshot for {code_system}")
    raw = gzip.decompress((SNAPSHOT_DIR / entry["file"]).read_bytes()).decode("utf-8")
    if code_system == "ICD10CM":
        rows = sources.parse_icd10cm_order_file(raw)
    else:
        rows = [
            sources.CodeRow(item["code"], item["code"], item["short"], item["long"], True,
                            date.fromisoformat(item["from"]) if item.get("from") else None,
                            date.fromisoformat(item["to"]) if item.get("to") else None)
            for item in json.loads(raw)
        ]
    return sources.Release(code_system, entry["release_id"], date.fromisoformat(entry["effective_from"]),
                           f"{entry['source']} (bundled snapshot)", entry["source_url"], rows)


def export() -> dict:
    """Download the latest official releases and write them to data/reference/."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {}

    ref = sources.discover_icd10cm_releases()
    ref = [item for item in ref if not item.release_id.endswith("UPDATE")][-1]
    url = sources._find_code_descriptions_zip(ref.folder)
    response = sources._session().get(url, timeout=sources.TIMEOUT)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(f"order-{ref.year}.txt")
                    or (n.lower().endswith(".txt") and "order" in n.lower() and "addenda" not in n.lower()))
        text = archive.read(name).decode("latin-1")
    icd_file = f"icd10cm-order-{ref.release_id}.txt.gz"
    (SNAPSHOT_DIR / icd_file).write_bytes(gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0))
    manifest["ICD10CM"] = {"release_id": ref.release_id, "effective_from": ref.effective_from.isoformat(),
                           "file": icd_file, "source": "CDC/NCHS ICD-10-CM", "source_url": url,
                           "codes": len(sources.parse_icd10cm_order_file(text))}

    hcpcs = sources.fetch_hcpcs_release()
    payload = [{"code": r.code, "short": r.short_description, "long": r.long_description,
                "from": r.source_valid_from.isoformat() if r.source_valid_from else None,
                "to": r.source_valid_to.isoformat() if r.source_valid_to else None} for r in hcpcs.rows]
    hcpcs_file = f"hcpcs-{hcpcs.release_id}.json.gz"
    (SNAPSHOT_DIR / hcpcs_file).write_bytes(
        gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), compresslevel=9, mtime=0))
    manifest["HCPCS"] = {"release_id": hcpcs.release_id, "effective_from": hcpcs.effective_from.isoformat(),
                         "file": hcpcs_file, "source": "CMS HCPCS via NLM Clinical Tables",
                         "source_url": sources.NLM_HCPCS_URL, "codes": len(payload)}
    for old in SNAPSHOT_DIR.glob("*.gz"):
        if old.name not in {icd_file, hcpcs_file}:
            old.unlink()
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["export", "show"])
    args = parser.parse_args(argv)
    manifest = export() if args.action == "export" else _manifest()
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
