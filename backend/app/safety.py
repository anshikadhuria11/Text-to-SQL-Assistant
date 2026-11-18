"""Parse, check, and rewrite SQL before it can reach SQLite."""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify

from app.db import SAFE_FUNCTIONS, reference_date

QUERY_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except)
FORBIDDEN_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Command,
    exp.Pragma,
    exp.Attach,
    exp.Detach,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Set,
    exp.TruncateTable,
    exp.Merge,
    exp.Use,
)


class SafetyError(Exception):
    """A statement that must not run. `destructive` skips the repair loop."""

    def __init__(self, reason: str, *, destructive: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.destructive = destructive


def _statements(sql: str) -> list[exp.Expression]:
    try:
        trees = sqlglot.parse(sql, read="sqlite")
    except ParseError as exc:
        raise SafetyError("SQL could not be parsed.") from exc
    return [tree for tree in trees if tree is not None and not isinstance(tree, exp.Semicolon)]


def _is_query(node: exp.Expression) -> bool:
    if isinstance(node, QUERY_TYPES):
        return True
    if isinstance(node, exp.With):
        inner = node.this
        return inner is not None and _is_query(inner)
    return False


def _cte_names(node: exp.Expression) -> set[str]:
    names = set()
    for cte in node.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            names.add(alias.lower())
    return names


def _check_functions(node: exp.Expression) -> None:
    for item in node.walk():
        if isinstance(item, FORBIDDEN_TYPES):
            label = type(item).__name__.upper()
            raise SafetyError(f"{label} statements are blocked.", destructive=True)
        if isinstance(item, exp.Anonymous):
            name = str(item.this or "").lower()
            if name not in SAFE_FUNCTIONS:
                raise SafetyError(f"Function {name or 'unknown'} is not allowed.", destructive=True)


def _check_tables(node: exp.Expression, schema: dict[str, dict[str, str]]) -> None:
    known = {name.lower(): name for name in schema}
    ctes = _cte_names(node)
    for table in node.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        lowered = name.lower()
        if lowered in ctes:
            continue
        if lowered.startswith("sqlite_"):
            raise SafetyError(f"Access to {name} is not allowed.")
        if lowered not in known:
            raise SafetyError(f"Unknown table '{name}'.")


def _uses_moving_clock(node: exp.Expression) -> bool:
    for item in node.walk():
        if isinstance(item, (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime)):
            return True
        if isinstance(item, exp.Literal) and item.is_string and str(item.this).lower() == "now":
            return True
        if isinstance(item, (exp.Date, exp.Datetime, exp.Timestamp)):
            this = item.args.get("this")
            expressions = item.args.get("expressions") or []
            if this is None and not expressions:
                return True
    return False


def _apply_limit(node: exp.Expression, max_rows: int) -> str:
    limit = node.args.get("limit")
    if limit is None or limit.expression is None:
        node.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return f"LIMIT {max_rows} added"
    value = limit.expression
    if isinstance(value, exp.Literal) and value.is_int and int(value.this) > max_rows:
        limit.set("expression", exp.Literal.number(max_rows))
        return f"LIMIT clamped to {max_rows}"
    return "LIMIT left unchanged"


def validate_sql(sql: str, schema: dict[str, dict[str, str]], *, max_rows: int) -> tuple[str, str]:
    """Return regenerated SQL and a short note describing the limit decision."""
    text = (sql or "").strip()
    if not text:
        raise SafetyError("SQL is empty.")
    if len(text) > 4000:
        raise SafetyError("SQL is longer than 4000 characters.")

    statements = _statements(text)
    if len(statements) != 1:
        destructive = any(
            (not _is_query(item)) or any(isinstance(node, FORBIDDEN_TYPES) for node in item.walk())
            for item in statements
        )
        raise SafetyError("Multiple statements are not allowed.", destructive=destructive)

    statement = statements[0]
    if not _is_query(statement):
        raise SafetyError("Only a single read-only SELECT is allowed.", destructive=True)

    _check_functions(statement)
    _check_tables(statement, schema)
    try:
        qualified = qualify(
            statement.copy(),
            schema=schema,
            dialect="sqlite",
            validate_qualify_columns=True,
        )
    except OptimizeError as exc:
        message = str(exc).split("\n", 1)[0]
        raise SafetyError(message or "A column or table could not be resolved.") from exc
    except SqlglotError as exc:
        raise SafetyError("SQL could not be checked against the schema.") from exc

    if _uses_moving_clock(qualified):
        when = reference_date()
        raise SafetyError(f"Use the fixed reference date {when} instead of the current time.")

    limit_note = _apply_limit(qualified, max_rows)
    rewritten = qualified.sql(dialect="sqlite", pretty=True)
    return rewritten, limit_note
