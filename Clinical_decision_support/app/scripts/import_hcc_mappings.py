"""Import the official CMS ICD-10-CM -> CMS-HCC mapping into data/rules/hcc_v28_mappings.csv.

Download the "2026 Midyear/Final ICD-10 Mappings" from
https://www.cms.gov/medicare/payment/medicare-advantage-rates-statistics/risk-adjustment/2026-model-software/icd-10-mappings
(a ZIP with an Excel or CSV file), then run from the Clinical_decision_support folder:

    .venv\\Scripts\\python -m app.scripts.import_hcc_mappings path\\to\\2026-midyear-final-icd-10-mappings.zip

The coding screen picks the new mapping up on the next refresh (no restart). Only codes with a V28
payment HCC are kept.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd

from frontend.hcc import OFFICIAL_MAPPING

_CODE_COLUMN = re.compile(r"diagnosis\s*code|icd.?10.?cm\s*code|^code$", re.I)
_V28_COLUMN = re.compile(r"v28", re.I)


def _frames(path: Path) -> list[pd.DataFrame]:
    if path.suffix.lower() == ".zip":
        frames = []
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                data = archive.read(name)
                if name.lower().endswith((".xlsx", ".xls")):
                    frames += _excel(io.BytesIO(data))
                elif name.lower().endswith((".csv", ".txt")):
                    frames.append(pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding_errors="replace"))
        return frames
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return _excel(path)
    return [pd.read_csv(path, dtype=str, keep_default_na=False, encoding_errors="replace")]


def _excel(source) -> list[pd.DataFrame]:
    frames = []
    for sheet, raw in pd.read_excel(source, sheet_name=None, header=None, dtype=str).items():
        raw = raw.fillna("")
        # CMS files put a title block above the header row: find the row that names the diagnosis code.
        header_row = next((index for index, row in raw.iterrows() if any(_CODE_COLUMN.search(str(cell)) for cell in row)), None)
        if header_row is None:
            continue
        frame = raw.iloc[header_row + 1:].copy()
        frame.columns = [str(cell).strip() for cell in raw.iloc[header_row]]
        frames.append(frame)
    return frames


def extract(path: Path) -> list[tuple[str, str]]:
    rows: dict[str, str] = {}
    for frame in _frames(path):
        code_col = next((column for column in frame.columns if _CODE_COLUMN.search(str(column))), None)
        v28_cols = [column for column in frame.columns if _V28_COLUMN.search(str(column)) and "label" not in str(column).lower()]
        if code_col is None or not v28_cols:
            continue
        for _, row in frame.iterrows():
            code = str(row[code_col]).replace(".", "").strip().upper()
            hcc = next((str(row[column]).strip() for column in v28_cols if re.fullmatch(r"\d{1,3}", str(row[column]).strip())), "")
            if code and hcc:
                rows.setdefault(code, hcc)
    return sorted(rows.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="CMS mapping ZIP, XLSX or CSV")
    args = parser.parse_args(argv)
    if not args.source.is_file():
        print(f"File not found: {args.source}", file=sys.stderr)
        return 1
    rows = extract(args.source)
    if not rows:
        print("No ICD-10-CM -> V28 HCC rows found; is this the CMS ICD-10 mappings file?", file=sys.stderr)
        return 1
    OFFICIAL_MAPPING.parent.mkdir(parents=True, exist_ok=True)
    with OFFICIAL_MAPPING.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "hcc"])
        writer.writerows(rows)
    print(f"Imported {len(rows)} ICD-10-CM codes with a V28 payment HCC into {OFFICIAL_MAPPING}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
