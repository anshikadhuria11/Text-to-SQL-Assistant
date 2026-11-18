"""State carried between workflow steps."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AgentState(TypedDict, total=False):
    question: str
    history: list[dict[str, Any]]
    schema_text: str
    selected_tables: list[str]
    intent: str
    clarification: str | None
    sql: str | None
    reasoning: str | None
    validated_sql: str | None
    error: str | None
    attempts: int
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    answer: str
    chart: dict[str, Any]
    followups: list[str]
    trace: Annotated[list[dict[str, Any]], operator.add]
    llm_calls: int
    status: str
    block_reason: str | None
    limit_note: str
