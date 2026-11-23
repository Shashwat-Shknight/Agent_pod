"""EHR schema repository: the workbook-shaped patient chart.

The EHR service works on the whole chart document (``{"sheets": {...}, ...}``). Here each sheet
is one row in EHR.SHEETS and only sheets whose content changed are written, inside a single
transaction. Reads are cached per process and re-validated against a cheap version stamp.
"""

from __future__ import annotations

import copy
import hashlib
import threading
import time
from typing import Any

from app.db import client

STORE_KEY = "primary"
REVALIDATE_SECONDS = 5

_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None
_CACHE_STAMP: tuple | None = None
_CHECKED_AT = 0.0


def _sheet_hash(sheet: dict[str, Any]) -> str:
    return hashlib.sha256(client.to_json(sheet).encode("utf-8")).hexdigest()


def _stamp() -> tuple:
    rows = client.query(
        "SELECT COUNT(*) AS N, MAX(UPDATED_AT)::STRING AS T, "
        "HASH_AGG(CONTENT_HASH) AS H FROM EHR.SHEETS"
    )
    row = rows[0] if rows else {}
    return (row.get("N"), row.get("T"), row.get("H"))


def _ordered(record: Any, headers: list[str]) -> Any:
    """VARIANT objects come back with sorted keys; restore the sheet's column order."""
    if not isinstance(record, dict):
        return record
    ordered = {header: record[header] for header in headers if header in record}
    ordered.update({key: value for key, value in record.items() if key not in ordered})
    return ordered


def _fetch() -> dict[str, Any] | None:
    rows = client.query(
        "SELECT SHEET_NAME, KIND, HEADERS, RECORDS, RAW_ROWS FROM EHR.SHEETS ORDER BY SHEET_ORDER, SHEET_NAME"
    )
    if not rows:
        return None
    meta = client.query("SELECT EXTRA FROM EHR.STORE_METADATA WHERE STORE_KEY = %s", (STORE_KEY,))
    payload = dict(client.variant(meta[0]["EXTRA"]) or {}) if meta else {}
    sheets: dict[str, Any] = {}
    for row in rows:
        if row["KIND"] == "table":
            headers = client.variant(row["HEADERS"]) or []
            sheets[row["SHEET_NAME"]] = {
                "headers": headers,
                "records": [_ordered(record, headers) for record in client.variant(row["RECORDS"]) or []],
            }
        else:
            sheets[row["SHEET_NAME"]] = {"raw_rows": client.variant(row["RAW_ROWS"]) or []}
    payload["sheets"] = sheets
    return payload


def load_payload() -> dict[str, Any] | None:
    """Full chart document, or None when the EHR has not been seeded yet."""
    global _CACHE, _CACHE_STAMP, _CHECKED_AT
    with _LOCK:
        now = time.monotonic()
        if _CACHE is not None and now - _CHECKED_AT < REVALIDATE_SECONDS:
            return copy.deepcopy(_CACHE)
        stamp = _stamp()
        if _CACHE is None or stamp != _CACHE_STAMP:
            _CACHE = _fetch()
            _CACHE_STAMP = stamp
        _CHECKED_AT = now
        return copy.deepcopy(_CACHE)


def save_payload(payload: dict[str, Any]) -> dict[str, int]:
    """Persist the chart document; writes only the sheets that changed."""
    global _CACHE, _CACHE_STAMP, _CHECKED_AT
    sheets = payload.get("sheets")
    if not isinstance(sheets, dict):
        raise client.StorageError("EHR payload must contain a 'sheets' object")
    with _LOCK:
        previous = _CACHE if _CACHE is not None else (_fetch() or {"sheets": {}})
        old_sheets = previous.get("sheets", {})
        changed = [
            (order, name, sheet) for order, (name, sheet) in enumerate(sheets.items())
            if name not in old_sheets or _sheet_hash(old_sheets[name]) != _sheet_hash(sheet)
            or list(old_sheets).index(name) != order
        ]
        removed = [name for name in old_sheets if name not in sheets]
        extra = {key: value for key, value in payload.items() if key != "sheets"}
        extra_changed = client.to_json(extra) != client.to_json(
            {key: value for key, value in previous.items() if key != "sheets"}
        )
        if not (changed or removed or extra_changed):
            return {"changed": 0, "removed": 0}

        rows = []
        for order, name, sheet in changed:
            is_table = "records" in sheet
            rows.append({
                "name": name,
                "order": order,
                "kind": "table" if is_table else "raw",
                "headers": sheet.get("headers") if is_table else None,
                "records": sheet.get("records") if is_table else None,
                "raw_rows": None if is_table else sheet.get("raw_rows", []),
                "row_count": len(sheet.get("records") or sheet.get("raw_rows") or []),
                "hash": _sheet_hash(sheet),
            })
        with client.transaction() as cursor:
            if rows:
                cursor.execute(
                    "MERGE INTO EHR.SHEETS t USING ("
                    "  SELECT f.value:name::STRING AS SHEET_NAME, f.value:order::NUMBER AS SHEET_ORDER, "
                    "         f.value:kind::STRING AS KIND, f.value:headers AS HEADERS, f.value:records AS RECORDS, "
                    "         f.value:raw_rows AS RAW_ROWS, f.value:row_count::NUMBER AS ROW_COUNT, "
                    "         f.value:hash::STRING AS CONTENT_HASH "
                    "    FROM TABLE(FLATTEN(INPUT => PARSE_JSON(%s))) f"
                    ") s ON t.SHEET_NAME = s.SHEET_NAME "
                    "WHEN MATCHED THEN UPDATE SET SHEET_ORDER = s.SHEET_ORDER, KIND = s.KIND, HEADERS = s.HEADERS, "
                    "  RECORDS = s.RECORDS, RAW_ROWS = s.RAW_ROWS, ROW_COUNT = s.ROW_COUNT, "
                    "  CONTENT_HASH = s.CONTENT_HASH, UPDATED_AT = CURRENT_TIMESTAMP() "
                    "WHEN NOT MATCHED THEN INSERT (SHEET_NAME, SHEET_ORDER, KIND, HEADERS, RECORDS, RAW_ROWS, "
                    "  ROW_COUNT, CONTENT_HASH) VALUES (s.SHEET_NAME, s.SHEET_ORDER, s.KIND, s.HEADERS, s.RECORDS, "
                    "  s.RAW_ROWS, s.ROW_COUNT, s.CONTENT_HASH)",
                    (client.to_json(rows),),
                )
            for name in removed:
                cursor.execute("DELETE FROM EHR.SHEETS WHERE SHEET_NAME = %s", (name,))
            if extra_changed:
                cursor.execute(
                    "MERGE INTO EHR.STORE_METADATA t USING (SELECT %s AS K, PARSE_JSON(%s) AS V) s "
                    "ON t.STORE_KEY = s.K "
                    "WHEN MATCHED THEN UPDATE SET EXTRA = s.V, FORMAT = s.V:format::STRING, "
                    "  SOURCE_WORKBOOK = COALESCE(s.V:source_workbook::STRING, s.V:workbook:source_file::STRING), "
                    "  SOURCE_CREATED_AT = s.V:created_at::STRING, UPDATED_AT = CURRENT_TIMESTAMP() "
                    "WHEN NOT MATCHED THEN INSERT (STORE_KEY, FORMAT, SOURCE_WORKBOOK, SOURCE_CREATED_AT, EXTRA) "
                    "  VALUES (s.K, s.V:format::STRING, "
                    "  COALESCE(s.V:source_workbook::STRING, s.V:workbook:source_file::STRING), "
                    "  s.V:created_at::STRING, s.V)",
                    (STORE_KEY, client.to_json(extra)),
                )
        _CACHE = copy.deepcopy(payload)
        _CACHE_STAMP = _stamp()
        _CHECKED_AT = time.monotonic()
        return {"changed": len(rows), "removed": len(removed)}
