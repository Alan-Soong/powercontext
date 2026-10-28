"""Runtime configuration for WorkPulse.

Every value can be overridden with an environment variable; a local ``.env``
file next to the project root is loaded when present. No secret is ever
committed: ``.env.example`` only contains placeholders.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (keeps the project free of extra dependencies)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- seekdb (MySQL protocol) ---------------------------------------
    seekdb_host: str = field(default_factory=lambda: _env("WP_SEEKDB_HOST", "127.0.0.1"))
    seekdb_port: int = field(default_factory=lambda: _env_int("WP_SEEKDB_PORT", 2881))
    seekdb_user: str = field(default_factory=lambda: _env("WP_SEEKDB_USER", "root"))
    seekdb_password: str = field(default_factory=lambda: _env("WP_SEEKDB_PASSWORD", ""))
    seekdb_database: str = field(default_factory=lambda: _env("WP_SEEKDB_DATABASE", "workpulse"))

    # --- PowerContext ---------------------------------------------------
    pc_url: str = field(default_factory=lambda: _env("WP_PC_URL", "http://127.0.0.1:8000").rstrip("/"))
    pc_token: str = field(default_factory=lambda: _env("WP_PC_TOKEN", ""))
    pc_scope_prefix: str = field(default_factory=lambda: _env("WP_PC_SCOPE_PREFIX", "workpulse"))
    pc_timeout: float = field(default_factory=lambda: float(_env("WP_PC_TIMEOUT", "20")))

    # --- embeddings ------------------------------------------------------
    embedding_provider: str = field(default_factory=lambda: _env("WP_EMBEDDING_PROVIDER", "hash").lower())
    embedding_dim: int = field(default_factory=lambda: _env_int("WP_EMBEDDING_DIM", 384))
    embedding_base_url: str = field(default_factory=lambda: _env("WP_EMBEDDING_BASE_URL", ""))
    embedding_api_key: str = field(default_factory=lambda: _env("WP_EMBEDDING_API_KEY", ""))
    embedding_model: str = field(default_factory=lambda: _env("WP_EMBEDDING_MODEL", "text-embedding-3-small"))

    # --- retrieval defaults ---------------------------------------------
    ft_weight: float = field(default_factory=lambda: float(_env("WP_FT_WEIGHT", "0.45")))
    vec_weight: float = field(default_factory=lambda: float(_env("WP_VEC_WEIGHT", "0.55")))
    chunk_chars: int = field(default_factory=lambda: _env_int("WP_CHUNK_CHARS", 320))
    chunk_overlap: int = field(default_factory=lambda: _env_int("WP_CHUNK_OVERLAP", 60))

    # --- server ----------------------------------------------------------
    host: str = field(default_factory=lambda: _env("WP_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("WP_PORT", 8077))
    log_dir: Path = field(default_factory=lambda: Path(_env("WP_LOG_DIR", str(PROJECT_ROOT / "logs"))))


settings = Settings()
settings.log_dir.mkdir(parents=True, exist_ok=True)
