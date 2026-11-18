"""Read-only SQLite access, schema cache, and guarded query execution."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.config import settings

SAFE_FUNCTIONS = frozenset(
    {
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "round",
        "abs",
        "length",
        "lower",
        "upper",
        "substr",
        "trim",
        "replace",
        "coalesce",
        "ifnull",
        "nullif",
        "iif",
        "cast",
        "date",
        "datetime",
        "strftime",
        "julianday",
        "typeof",
        "group_concat",
        "total",
    }
)

_resolved_path: Path | None = None
_schema_cache: dict[str, Any] | None = None
_meta_cache: dict[str, Any] | None = None


class QueryTimeout(sqlite3.OperationalError):
    """Raised when a statement exceeds the wall-clock limit."""


def load_meta() -> dict[str, Any]:
    """Read store_meta.json once and cache it."""
    global _meta_cache
    if _meta_cache is None:
        _meta_cache = json.loads(settings.meta_path.read_text(encoding="utf-8"))
    return _meta_cache


def reference_date() -> str:
    """Fixed store date from the seed metadata."""
    return str(load_meta()["reference_date"])


def data_hash() -> str:
    """First 12 hex characters of the seeded content hash."""
    return str(load_meta()["content_sha256"])[:12]


def resolved_db_path() -> Path:
    """Path opened for reads. Copies to /tmp once if the bundle path is not readable."""
    global _resolved_path
    if _resolved_path is not None:
        return _resolved_path
    source = settings.db_path
    try:
        probe = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        probe.execute("SELECT 1")
        probe.close()
        _resolved_path = source
    except sqlite3.Error:
        destination = Path("/tmp") / source.name
        if not destination.exists():
            shutil.copy(source, destination)
        _resolved_path = destination
    return _resolved_path


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _src: str | None):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        name = (arg2 or arg1 or "").lower()
        if name in SAFE_FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


def open_connection(
    path: Path | None = None,
    *,
    read_only_uri: bool = True,
    query_only: bool = True,
    authorizer: bool = True,
    timeout_s: float | None = None,
) -> sqlite3.Connection:
    """Open a connection. The API always enables every guard."""
    target = path or resolved_db_path()
    if read_only_uri:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(target)
    if query_only:
        conn.execute("PRAGMA query_only = ON")
    if authorizer:
        conn.set_authorizer(_authorizer)
    if timeout_s is not None:
        deadline = time.monotonic() + timeout_s

        def _progress() -> int:
            return 1 if time.monotonic() >= deadline else 0

        conn.set_progress_handler(_progress, 10000)
    return conn


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str)):
        if isinstance(value, str) and len(value) > 300:
            return value[:300]
        return value
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
        return text[:300]
    return str(value)[:300]


def execute_select(
    sql: str,
    *,
    max_rows: int | None = None,
    timeout_s: float | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Run one checked statement and return columns, rows, and a truncation flag."""
    limit = settings.max_rows if max_rows is None else max_rows
    timeout = settings.query_timeout_s if timeout_s is None else timeout_s
    started = time.perf_counter()
    conn = open_connection(path=path, timeout_s=timeout)
    try:
        cursor = conn.execute(sql)
        columns = [item[0] for item in cursor.description or []]
        fetched = cursor.fetchmany(limit + 1)
        truncated = len(fetched) > limit
        if truncated:
            fetched = fetched[:limit]
        rows = [list(_json_value(cell) for cell in row) for row in fetched]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "columns": columns,
            "rows": rows,
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
        }
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise QueryTimeout("Query exceeded the time limit.") from exc
        raise
    finally:
        conn.close()


def _introspect(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        tables = []
        mapping: dict[str, dict[str, str]] = {}
        for name in names:
            info = conn.execute(f"PRAGMA table_info({name})").fetchall()
            fks = conn.execute(f"PRAGMA foreign_key_list({name})").fetchall()
            fk_by_column = {row[3]: f"{row[2]}.{row[4]}" for row in fks}
            columns = []
            mapping[name] = {}
            for row in info:
                col_name = row[1]
                col_type = row[2] or "TEXT"
                mapping[name][col_name] = col_type
                columns.append(
                    {
                        "name": col_name,
                        "type": col_type,
                        "pk": bool(row[5]),
                        "fk": fk_by_column.get(col_name),
                    }
                )
            count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            tables.append({"name": name, "columns": columns, "row_count": count})
        return {"tables": tables, "mapping": mapping}
    finally:
        conn.close()


def get_schema() -> dict[str, Any]:
    """Cached introspection from a trusted connection that has no authorizer."""
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = _introspect(resolved_db_path())
    return _schema_cache


def prepare() -> None:
    """Resolve the database file and warm the schema cache."""
    resolved_db_path()
    load_meta()
    get_schema()


def reset_caches() -> None:
    """Clear process caches. Used when tests point at a different file."""
    global _resolved_path, _schema_cache, _meta_cache
    _resolved_path = None
    _schema_cache = None
    _meta_cache = None
