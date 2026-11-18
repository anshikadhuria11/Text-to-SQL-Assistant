"""Runtime settings loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass
class Settings:
    """Values that control the API, the database, and the model."""

    groq_api_key: str
    groq_model: str
    groq_fallback_model: str
    db_path: Path
    meta_path: Path
    max_rows: int
    query_timeout_s: float
    max_repairs: int
    max_question_chars: int
    history_turns: int
    rate_limit_per_min: int
    schema_token_budget_chars: int
    cors_origins: list[str]
    max_llm_calls: int = 6
    max_sql_chars: int = 4000
    history_char_cap: int = 600


def _resolve_db_path() -> Path:
    raw = os.getenv("DB_PATH", "backend/data/store.db")
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def load_settings() -> Settings:
    """Build settings from the environment, applying documented defaults."""
    db_path = _resolve_db_path()
    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173")
    return Settings(
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        groq_fallback_model=os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b"),
        db_path=db_path,
        meta_path=db_path.with_name("store_meta.json"),
        max_rows=_int("MAX_ROWS", 200),
        query_timeout_s=float(os.getenv("QUERY_TIMEOUT_S", "5")),
        max_repairs=_int("MAX_REPAIRS", 2),
        max_question_chars=_int("MAX_QUESTION_CHARS", 500),
        history_turns=_int("HISTORY_TURNS", 6),
        rate_limit_per_min=_int("RATE_LIMIT_PER_MIN", 10),
        schema_token_budget_chars=_int("SCHEMA_TOKEN_BUDGET_CHARS", 6000),
        cors_origins=[item.strip() for item in origins.split(",") if item.strip()],
    )


settings = load_settings()
