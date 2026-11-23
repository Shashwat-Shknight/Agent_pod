"""Storage configuration.

Settings come from environment variables, optionally loaded from the project ``.env`` file
(never committed). ``STORAGE_BACKEND`` selects where application data lives:

* ``snowflake`` - production: every write goes to Snowflake. If Snowflake is unreachable the
  operation fails loudly; nothing is silently written to local disk instead.
* ``json``      - legacy local files; used by the unit tests and for offline development.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

SNOWFLAKE = "snowflake"
JSON = "json"


def _load_env_file() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is optional; plain env vars still work
        return
    if ENV_FILE.is_file():
        load_dotenv(ENV_FILE, override=False)


@dataclass(frozen=True)
class SnowflakeSettings:
    account: str
    user: str
    private_key_path: str
    private_key_passphrase: str | None
    role: str
    warehouse: str
    database: str
    login_timeout: int
    network_timeout: int
    pool_size: int
    query_tag: str

    def missing(self) -> list[str]:
        required = {
            "SNOWFLAKE_ACCOUNT": self.account,
            "SNOWFLAKE_USER": self.user,
            "SNOWFLAKE_PRIVATE_KEY_PATH": self.private_key_path,
            "SNOWFLAKE_ROLE": self.role,
            "SNOWFLAKE_WAREHOUSE": self.warehouse,
            "SNOWFLAKE_DATABASE": self.database,
        }
        return [name for name, value in required.items() if not value]


def _snowflake_settings(prefix: str = "SNOWFLAKE_") -> SnowflakeSettings:
    def env(name: str, default: str = "") -> str:
        return os.environ.get(prefix + name, os.environ.get("SNOWFLAKE_" + name, default)).strip()

    key_path = env("PRIVATE_KEY_PATH")
    return SnowflakeSettings(
        account=env("ACCOUNT"),
        user=env("USER"),
        private_key_path=os.path.expandvars(os.path.expanduser(key_path)) if key_path else "",
        private_key_passphrase=env("PRIVATE_KEY_PASSPHRASE") or None,
        role=env("ROLE"),
        warehouse=env("WAREHOUSE"),
        database=env("DATABASE", "NUUCARE_DB"),
        login_timeout=int(env("LOGIN_TIMEOUT", "30")),
        network_timeout=int(env("NETWORK_TIMEOUT", "60")),
        pool_size=max(1, int(env("POOL_SIZE", "4"))),
        query_tag=env("QUERY_TAG", "nuucare-app"),
    )


@lru_cache(maxsize=1)
def storage_backend() -> str:
    _load_env_file()
    backend = os.environ.get("STORAGE_BACKEND", JSON).strip().lower()
    if backend not in {SNOWFLAKE, JSON}:
        raise ValueError(f"STORAGE_BACKEND must be 'snowflake' or 'json', not {backend!r}")
    return backend


def use_snowflake() -> bool:
    return storage_backend() == SNOWFLAKE


@lru_cache(maxsize=2)
def snowflake_settings(profile: str = "app") -> SnowflakeSettings:
    """``app`` = runtime service user; ``deploy`` = migration user (SNOWFLAKE_DEPLOY_* overrides)."""
    _load_env_file()
    return _snowflake_settings("SNOWFLAKE_DEPLOY_" if profile == "deploy" else "SNOWFLAKE_")


def reset_cache() -> None:
    """For tests: re-read the environment."""
    storage_backend.cache_clear()
    snowflake_settings.cache_clear()
