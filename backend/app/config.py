"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """Minimal .env loader so local runs work without extra dependencies.

    Existing environment variables always win over the file.
    """
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    db_url: str = field(
        default_factory=lambda: os.environ.get(
            "STASHTRACE_DB_URL", f"sqlite:///{(REPO_ROOT / 'var' / 'stashtrace.db').as_posix()}"
        )
    )
    demo_password: str = field(default_factory=lambda: os.environ.get("STASHTRACE_DEMO_PASSWORD", ""))
    token_ttl_hours: int = field(default_factory=lambda: int(os.environ.get("STASHTRACE_TOKEN_TTL_HOURS", "12")))
    worker_poll_seconds: float = field(
        default_factory=lambda: float(os.environ.get("STASHTRACE_WORKER_POLL_SECONDS", "2"))
    )
    import_chunk_size: int = field(default_factory=lambda: int(os.environ.get("STASHTRACE_IMPORT_CHUNK", "5000")))
    # When true the API processes queued jobs in-process (handy for tests / a single-process demo).
    run_jobs_inline: bool = field(
        default_factory=lambda: os.environ.get("STASHTRACE_RUN_JOBS_INLINE", "false").lower() == "true"
    )
    sqlite_cache_mb: int = field(default_factory=lambda: int(os.environ.get("STASHTRACE_SQLITE_CACHE_MB", "256")))
    sqlite_mmap_mb: int = field(default_factory=lambda: int(os.environ.get("STASHTRACE_SQLITE_MMAP_MB", "1024")))
    log_level: str = field(default_factory=lambda: os.environ.get("STASHTRACE_LOG_LEVEL", "INFO"))
    frontend_dir: Path = REPO_ROOT / "frontend"
    max_upload_mb: int = field(default_factory=lambda: int(os.environ.get("STASHTRACE_MAX_UPLOAD_MB", "512")))


settings = Settings()
