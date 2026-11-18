"""Check a chart spec and fall back to a deterministic choice."""

from __future__ import annotations

import re
from typing import Any

ALLOWED = {"bar", "line", "stat", "none"}
DATE_VALUE = re.compile(r"^\d{4}(-\d{2}){0,2}$")
DATE_NAME = ("date", "month", "day", "year", "week")


def _column_values(rows: list[list[Any]], index: int) -> list[Any]:
    return [row[index] for row in rows if index < len(row)]


def _is_numeric(values: list[Any]) -> bool:
    seen = False
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        seen = True
    return seen


def _is_date_like(name: str, values: list[Any]) -> bool:
    if any(part in name.lower() for part in DATE_NAME):
        return True
    present = [value for value in values if value is not None]
    if not present:
        return False
    return all(isinstance(value, str) and DATE_VALUE.match(value) for value in present)


def heuristic_chart(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    """Pick bar, line, stat, or none from the shape of the result."""
    if not columns or not rows:
        return {"type": "none", "x": None, "y": [], "title": ""}
    numeric_indexes = [
        index
        for index, _name in enumerate(columns)
        if _is_numeric(_column_values(rows, index))
    ]
    if len(rows) == 1 and len(numeric_indexes) >= 1:
        name = columns[numeric_indexes[0]]
        return {"type": "stat", "x": None, "y": [name], "title": name}
    if len(numeric_indexes) >= 1:
        x_index = 0 if 0 not in numeric_indexes else None
        if x_index is None:
            for index in range(len(columns)):
                if index not in numeric_indexes:
                    x_index = index
                    break
        if x_index is not None:
            x_name = columns[x_index]
            y_name = columns[numeric_indexes[0]]
            x_values = _column_values(rows, x_index)
            if _is_date_like(x_name, x_values) and 2 <= len(rows) <= 50:
                return {"type": "line", "x": x_name, "y": [y_name], "title": y_name}
            if not _is_numeric(x_values) and 2 <= len(rows) <= 25:
                return {"type": "bar", "x": x_name, "y": [y_name], "title": y_name}
    return {"type": "none", "x": None, "y": [], "title": ""}


def _normalize_spec(spec: dict[str, Any] | None) -> dict[str, Any]:
    spec = spec or {}
    y_values = spec.get("y") or []
    if isinstance(y_values, str):
        y_values = [y_values]
    return {
        "type": spec.get("type") or "none",
        "x": spec.get("x"),
        "y": [str(item) for item in y_values],
        "title": spec.get("title") or "",
    }


def validate_chart(
    spec: dict[str, Any] | None,
    columns: list[str],
    rows: list[list[Any]],
) -> dict[str, Any]:
    """Accept a spec that matches the columns, otherwise use the heuristic."""
    chart = _normalize_spec(spec)
    if chart["type"] not in ALLOWED:
        return heuristic_chart(columns, rows)
    if chart["type"] == "none":
        return chart
    lookup = {name: index for index, name in enumerate(columns)}
    y_names = chart["y"]
    if not y_names or any(name not in lookup for name in y_names):
        return heuristic_chart(columns, rows)
    if any(not _is_numeric(_column_values(rows, lookup[name])) for name in y_names):
        return heuristic_chart(columns, rows)
    if chart["type"] == "stat":
        return chart
    x_name = chart["x"]
    if x_name not in lookup:
        return heuristic_chart(columns, rows)
    row_count = len(rows)
    if not 2 <= row_count <= 50:
        return heuristic_chart(columns, rows)
    if chart["type"] == "line" and not _is_date_like(x_name, _column_values(rows, lookup[x_name])):
        return heuristic_chart(columns, rows)
    return chart
