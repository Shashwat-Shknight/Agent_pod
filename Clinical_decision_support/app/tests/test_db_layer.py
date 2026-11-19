"""Unit tests for the Snowflake repositories with the client faked out (no network)."""

import json

import pytest

from app.db import access_store, client, encounters, settings


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))


@pytest.fixture
def fake_db(monkeypatch):
    log, responses = [], {}

    def query(sql, params=None, profile="app"):
        log.append((" ".join(sql.split()), params))
        for marker, rows in responses.items():
            if marker in sql:
                return rows() if callable(rows) else rows
        return []

    class _Tx:
        def __enter__(self):
            return FakeCursor(log)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(client, "query", query)
    monkeypatch.setattr(client, "execute", lambda sql, params=None, profile="app": query(sql, params) or 1)
    monkeypatch.setattr(client, "transaction", lambda profile="app": _Tx())
    return log, responses


def test_to_json_is_canonical_and_nan_safe():
    assert client.to_json({"b": 1, "a": float("nan")}) == '{"a": null, "b": 1}'


def test_variant_decodes_json_text():
    assert client.variant('{"a": [1, 2]}') == {"a": [1, 2]}
    assert client.variant(None) is None


def test_save_encounter_skips_unchanged_records(fake_db):
    log, responses = fake_db
    responses["UPSERT_ENCOUNTER"] = [{"UPSERT_ENCOUNTER": json.dumps({"status": "saved"})}]
    encounters._KNOWN_HASH.clear()
    record = {"id": "PT-1", "name": "Demo", "status": "Pending"}
    assert encounters.save_encounter(record, "doc") == "saved"
    assert encounters.save_encounter(dict(record), "doc") == "unchanged"
    record["status"] = "Approved"
    assert encounters.save_encounter(record, "doc") == "saved"
    calls = [params for sql, params in log if "UPSERT_ENCOUNTER" in sql]
    assert len(calls) == 2
    assert json.loads(calls[-1][0])["status"] == "Approved" and calls[-1][1] == "doc"


def test_save_encounter_requires_id(fake_db):
    with pytest.raises(client.StorageError):
        encounters.save_encounter({"name": "no id"})


def test_access_store_writes_only_changes(fake_db, monkeypatch):
    log, _ = fake_db
    baseline = {
        "version": 1,
        "users": [{"username": "alice", "role": "Doctor"}, {"username": "bob", "role": "Coder"}],
        "role_permissions": {"Doctor": {"Scribe": ["View"]}},
        "audit": [{"at": "t1", "actor": "alice", "action": "auth.sign_in", "detail": ""}],
    }
    monkeypatch.setattr(access_store, "_SNAPSHOT", json.loads(json.dumps(baseline)))
    monkeypatch.setattr(access_store, "_LOADED_AT", 10**12)

    store = json.loads(json.dumps(baseline))
    store["users"][1]["role"] = "Biller"
    store["audit"].append({"at": "t2", "actor": "alice", "action": "user.update", "detail": "bob"})
    access_store.save_store(store)

    user_merge = next(params for sql, params in log if "SECURITY.APP_USERS" in sql)
    assert [user["username"] for user in json.loads(user_merge[0])] == ["bob"]
    audit_merge = next(params for sql, params in log if "SECURITY.AUDIT_LOG" in sql)
    assert [event["action"] for event in json.loads(audit_merge[0])] == ["user.update"]
    assert not any("ROLE_PERMISSIONS" in sql for sql, _ in log)

    log.clear()
    access_store.save_store(store)  # nothing new -> no statements
    assert log == []


def test_identical_audit_events_get_distinct_ids():
    event = {"at": "t", "actor": "a", "action": "x", "detail": ""}
    first, second = access_store._event_ids([event, dict(event)])
    assert first != second


def test_backend_selection(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "snowflake")
    settings.reset_cache()
    assert settings.use_snowflake()
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    settings.reset_cache()
    with pytest.raises(ValueError):
        settings.storage_backend()
