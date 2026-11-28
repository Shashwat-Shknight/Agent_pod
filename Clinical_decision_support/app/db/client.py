"""Snowflake connection pool and query helpers.

* Key-pair authentication (service user, no password / MFA prompts).
* Small thread-safe pool: Streamlit serves each browser session on its own thread and the
  FastAPI backend runs requests concurrently, so connections are checked out per call.
* One automatic retry on dropped / expired connections.
* All values are bound as parameters (never string-formatted into SQL).
"""

from __future__ import annotations

import atexit
import json
import os
import logging
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from app.db.settings import SnowflakeSettings, snowflake_settings

log = logging.getLogger("nuucare.db")


class StorageError(RuntimeError):
    """Raised when the database cannot complete an operation."""


def _load_private_key(path: str, passphrase: str | None) -> bytes:
    from cryptography.hazmat.primitives import serialization

    key_file = Path(path)
    if not key_file.is_file():
        raise StorageError(f"Snowflake private key not found: {key_file}")
    key = serialization.load_pem_private_key(
        key_file.read_bytes(), password=passphrase.encode() if passphrase else None
    )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _connect(settings: SnowflakeSettings):
    import snowflake.connector

    missing = settings.missing()
    if missing:
        raise StorageError("Snowflake is not configured; missing " + ", ".join(missing) + " (see .env.example)")
    return snowflake.connector.connect(
        account=settings.account,
        user=settings.user,
        private_key=_load_private_key(settings.private_key_path, settings.private_key_passphrase),
        role=settings.role,
        warehouse=settings.warehouse,
        database=settings.database,
        login_timeout=settings.login_timeout,
        network_timeout=settings.network_timeout,
        client_session_keep_alive=True,
        enable_server_session_keep_alive_auto_detection=True,
        application="NuuCare",
        session_parameters={
            "QUERY_TAG": settings.query_tag,
            "TIMEZONE": "UTC",
            "STATEMENT_TIMEOUT_IN_SECONDS": 120,
        },
    )


def _is_connection_error(error: Exception) -> bool:
    from snowflake.connector import errors

    if isinstance(error, (errors.OperationalError, errors.InterfaceError)):
        return True
    text = str(error).lower()
    markers = ("session no longer exists", "authentication token has expired", "connection is closed",
               "master token", "re-authentication", "token has expired", "390114", "390112", "250001")
    return any(marker in text for marker in markers)


# Snowflake session tokens last about four hours; retire pooled connections well before that so a
# long-running server never hands out an expired session ("Master token can never be renewed").
MAX_CONNECTION_AGE_SECONDS = float(os.getenv("SNOWFLAKE_MAX_CONNECTION_AGE_MIN", "180")) * 60


class ConnectionPool:
    def __init__(self, settings: SnowflakeSettings):
        self.settings = settings
        self._idle: queue.LifoQueue = queue.LifoQueue(maxsize=settings.pool_size)
        self._slots = threading.BoundedSemaphore(settings.pool_size)

    def _acquire(self):
        if not self._slots.acquire(timeout=60):
            raise StorageError("Timed out waiting for a database connection")
        try:
            while True:
                try:
                    connection = self._idle.get_nowait()
                except queue.Empty:
                    connection = _connect(self.settings)
                    connection._nuucare_created = time.monotonic()
                    return connection
                age = time.monotonic() - getattr(connection, "_nuucare_created", 0.0)
                if not connection.is_closed() and age < MAX_CONNECTION_AGE_SECONDS:
                    return connection
                try:
                    connection.close()
                except Exception:  # noqa: BLE001 - expired or already closed
                    pass
        except Exception:
            self._slots.release()
            raise

    def _release(self, connection, broken: bool = False) -> None:
        try:
            if broken or connection.is_closed():
                try:
                    connection.close()
                except Exception:  # noqa: BLE001 - already broken
                    pass
            else:
                self._idle.put_nowait(connection)
        except queue.Full:
            connection.close()
        finally:
            self._slots.release()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        connection = self._acquire()
        broken = False
        try:
            yield connection
        except Exception as error:
            broken = _is_connection_error(error)
            raise
        finally:
            self._release(connection, broken)

    def close(self) -> None:
        while True:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                return
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - expired sessions may fail to close
                pass


_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()


@atexit.register
def _close_pools() -> None:
    for connection_pool in list(_POOLS.values()):
        connection_pool.close()


def pool(profile: str = "app") -> ConnectionPool:
    with _POOLS_LOCK:
        if profile not in _POOLS:
            _POOLS[profile] = ConnectionPool(snowflake_settings(profile))
        return _POOLS[profile]


def _run(operation, profile: str = "app"):
    """Run ``operation(connection)`` with one retry on a dropped connection."""
    for attempt in (1, 2):
        try:
            with pool(profile).connection() as connection:
                return operation(connection)
        except StorageError:
            raise
        except Exception as error:  # noqa: BLE001 - translated below
            if attempt == 1 and _is_connection_error(error):
                log.warning("Snowflake session expired or dropped, reconnecting: %s", error)
                pool(profile).close()  # every idle connection shares the same token age: start fresh
                time.sleep(0.5)
                continue
            raise StorageError(f"Snowflake operation failed: {error}") from error
    raise StorageError("Snowflake operation failed after retry")


def query(sql: str, params: Sequence[Any] | dict | None = None, profile: str = "app") -> list[dict[str, Any]]:
    """Execute one statement and return rows as dicts (column names upper-case)."""
    from snowflake.connector import DictCursor

    def operation(connection):
        with connection.cursor(DictCursor) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else []

    return _run(operation, profile)


def execute(sql: str, params: Sequence[Any] | dict | None = None, profile: str = "app") -> int:
    """Execute one statement; returns the affected row count."""

    def operation(connection):
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount or 0

    return _run(operation, profile)


def execute_many(sql: str, rows: Iterable[Sequence[Any]], profile: str = "app") -> int:
    rows = list(rows)
    if not rows:
        return 0

    def operation(connection):
        with connection.cursor() as cursor:
            cursor.executemany(sql, rows)
            return cursor.rowcount or 0

    return _run(operation, profile)


@contextmanager
def transaction(profile: str = "app") -> Iterator[Any]:
    """Explicit transaction on one connection: yields a cursor; commits or rolls back."""
    with pool(profile).connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN")
            yield cursor
            cursor.execute("COMMIT")
        except Exception as error:
            try:
                cursor.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - connection may be gone
                pass
            if isinstance(error, StorageError):
                raise
            raise StorageError(f"Snowflake transaction failed: {error}") from error
        finally:
            cursor.close()


def variant(value: Any) -> Any:
    """Snowflake returns VARIANT/OBJECT/ARRAY columns as JSON text; decode them."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def to_json(value: Any) -> str:
    """Canonical JSON (sorted keys) used for VARIANT binds and change hashing."""
    return json.dumps(_finite(value), sort_keys=True, ensure_ascii=False, default=_json_default, allow_nan=False)


def _finite(value: Any) -> Any:
    """NaN / Infinity are not valid JSON (Snowflake PARSE_JSON rejects them): map to null."""
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {str(key): _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (set, tuple)):
        return list(value)
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def ping() -> dict[str, Any]:
    rows = query(
        "SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_USER() AS USER_NAME, CURRENT_ROLE() AS ROLE_NAME, "
        "CURRENT_WAREHOUSE() AS WAREHOUSE, CURRENT_DATABASE() AS DATABASE_NAME, CURRENT_VERSION() AS VERSION"
    )
    return rows[0] if rows else {}
