"""Clinical Overview: KPIs, activity chart and the consultation queue."""

from datetime import datetime

import pandas as pd







DATE_FILTERS = ("All Time", "Today", "Last 7 Days", "Last 30 Days", "Custom Date Range")
STATUS_FILTERS = ("All Statuses", "Pending", "Approved")


def _record_date(record):
    try:
        return datetime.strptime(record.get("date", ""), "%d/%m/%Y").date()
    except (ValueError, TypeError):
        return None


def filter_records(records, query, date_filter, status_filter, custom_range=None, assignee=None, today=None):
    """Return records matching the search text, date window, status and (optionally) assignee."""
    today = today or datetime.now().date()
    matches = []
    for record in records:
        if assignee and record.get("assigned_to") != assignee:
            continue
        searchable = (
            f"{record.get('name', '')} {record.get('id', '')} {record.get('mrn', '')} {record.get('ehr_patient_id', '')}".lower()
        )
        if query and query not in searchable:
            continue
        if status_filter != "All Statuses" and record.get("status") != status_filter:
            continue
        record_date = _record_date(record)
        if record_date is None:
            continue
        age_days = (today - record_date).days
        if date_filter == "Today" and record_date != today:
            continue
        if date_filter == "Last 7 Days" and not 0 <= age_days <= 7:
            continue
        if date_filter == "Last 30 Days" and not 0 <= age_days <= 30:
            continue
        if date_filter == "Custom Date Range":
            if not (
                isinstance(custom_range, tuple) and len(custom_range) == 2 and custom_range[0] <= record_date <= custom_range[1]
            ):
                continue
        matches.append(record)
    return matches
