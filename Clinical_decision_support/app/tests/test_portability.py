"""A fresh clone must work without Hugging Face and without network access to CDC/NLM."""

import importlib
import sys

import pytest
import requests

from app.reference import snapshot, sync


def test_bundled_snapshot_is_complete():
    icd = snapshot.load("ICD10CM")
    hcpcs = snapshot.load("HCPCS")
    assert icd.release_id.startswith("FY") and len(icd.rows) > 90000
    assert sum(row.is_billable for row in icd.rows) > 70000
    assert any(row.code == "I1A.0" for row in icd.rows)
    assert len(hcpcs.rows) > 5000 and any(row.code == "J0013" for row in hcpcs.rows)


def test_offline_fallback_loads_snapshot_when_nothing_loaded(monkeypatch):
    applied, finished = [], []
    monkeypatch.setattr(sync, "apply_release", lambda release, run_id, actor: applied.append(release.release_id) or
                        {"codes": len(release.rows), "added": len(release.rows), "changed": 0, "retired": 0})
    monkeypatch.setattr(sync, "_finish_run", lambda *args, **kwargs: finished.append(args[1]))
    result = sync._offline_fallback("ICD10CM", "run-1", "test", [], requests.ConnectionError("blocked"))
    assert result["status"] == "SUCCEEDED" and applied == [snapshot.load("ICD10CM").release_id]
    kept = sync._offline_fallback("ICD10CM", "run-2", "test", [{"RELEASE_ID": "FY2027"}], requests.ConnectionError("x"))
    assert kept["status"] == "OFFLINE" and len(applied) == 1


def test_rag_modules_import_without_huggingface(monkeypatch):
    # Simulate a machine where the optional ML packages are not installed.
    for name in ("sentence_transformers", "chromadb"):
        monkeypatch.setitem(sys.modules, name, None)
    for module in ("app.rag.embeddings", "app.rag.vector_store", "app.rag.retriever", "app.scripts.build_kb"):
        sys.modules.pop(module, None)
        importlib.import_module(module)
    from app.rag import embeddings, vector_store

    assert embeddings.sentence_transformers_available() is False
    with pytest.raises(RuntimeError, match="requirements-ml.txt"):
        vector_store.get_collection()


def test_setup_init_generates_keys_env_and_bootstrap(tmp_path, monkeypatch):
    from app.db import setup

    monkeypatch.setattr(setup, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(setup, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(setup, "GENERATED", tmp_path / "local" / "bootstrap.sql")
    monkeypatch.setattr(setup, "_copy_to_clipboard", lambda text: False)
    assert setup.init("abcdefg-xy12345") == 0
    sql = (tmp_path / "local" / "bootstrap.sql").read_text(encoding="utf-8")
    assert "{{" not in sql and sql.count("RSA_PUBLIC_KEY = 'MII") == 2
    assert "SNOWFLAKE_ACCOUNT=ABCDEFG-XY12345" in (tmp_path / ".env").read_text(encoding="utf-8")
    first_key = (tmp_path / "keys" / "nuucare_app_svc.p8").read_bytes()
    assert setup.init("abcdefg-xy12345") == 0          # idempotent: keys are reused, never overwritten
    assert (tmp_path / "keys" / "nuucare_app_svc.p8").read_bytes() == first_key


@pytest.mark.parametrize("bad", ["https://abc.snowflakecomputing.com", "abc.us-east-1", "justname"])
def test_setup_init_rejects_bad_account(bad):
    from app.db import setup

    assert setup.init(bad) == 2
