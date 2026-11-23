"""Background code-set synchronisation for the API process.

Every REFERENCE_SYNC_INTERVAL_HOURS (default 24) the API checks CDC for a new ICD-10-CM release
and refreshes the HCPCS snapshot when it is older than a week. A SYNC_RUNS row guards against two
processes syncing at once. Disable with REFERENCE_AUTO_SYNC=0.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("nuucare.reference")
_started = threading.Event()
_stop = threading.Event()


def enabled() -> bool:
    from app.db.settings import use_snowflake

    return use_snowflake() and os.environ.get("REFERENCE_AUTO_SYNC", "1").strip() not in {"0", "false", "no"}


def _loop(initial_delay: float, interval: float) -> None:
    if _stop.wait(initial_delay):
        return
    while not _stop.is_set():
        try:
            from app.reference import sync

            results = sync.sync_all("scheduler")
            for result in results:
                log.info("Code-set sync %s: %s %s", result["system"], result["status"], result.get("message", ""))
            if any(r["system"] == "ICD10CM" and r["status"] == "SUCCEEDED" for r in results):
                import code_matcher

                code_matcher.load_icd10_data(force_reload=True)
        except Exception:  # noqa: BLE001 - keep the scheduler alive; the failure is in SYNC_RUNS
            log.exception("Scheduled code-set sync failed")
        _stop.wait(interval)


def start() -> bool:
    if not enabled() or _started.is_set():
        return False
    _started.set()
    hours = float(os.environ.get("REFERENCE_SYNC_INTERVAL_HOURS", "24"))
    threading.Thread(target=_loop, args=(30.0, hours * 3600), name="code-set-sync", daemon=True).start()
    log.info("Code-set auto-sync enabled (every %.0f h)", hours)
    return True


def stop() -> None:
    _stop.set()
