"""VBC.STATE_DOCUMENTS repository: value-based care workspace state documents.

The state carries a hash-chained audit log, so it is loaded from the exact JSON text that was
saved (STATE_TEXT); STATE_JSON holds the same document as VARIANT for SQL queries.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.db import client


def load(doc_key: str) -> dict[str, Any] | None:
    rows = client.query("SELECT STATE_TEXT FROM VBC.STATE_DOCUMENTS WHERE DOC_KEY = %s", (doc_key,))
    if not rows:
        return None
    state = json.loads(rows[0]["STATE_TEXT"])
    return state if isinstance(state, dict) else None


def save(doc_key: str, state: dict[str, Any], default: Callable[[Any], Any] | None = None) -> None:
    text = json.dumps(state, indent=None, default=default, ensure_ascii=False)
    client.execute(
        "MERGE INTO VBC.STATE_DOCUMENTS t USING ("
        "  SELECT %s AS K, %s::NUMBER AS R, %s AS T) s "
        "ON t.DOC_KEY = s.K "
        "WHEN MATCHED THEN UPDATE SET REVISION = s.R, STATE_TEXT = s.T, STATE_JSON = PARSE_JSON(s.T), "
        "  UPDATED_AT = CURRENT_TIMESTAMP() "
        "WHEN NOT MATCHED THEN INSERT (DOC_KEY, REVISION, STATE_TEXT, STATE_JSON) "
        "  VALUES (s.K, s.R, s.T, PARSE_JSON(s.T))",
        (doc_key, int(state.get("revision", 0) or 0), text),
    )
