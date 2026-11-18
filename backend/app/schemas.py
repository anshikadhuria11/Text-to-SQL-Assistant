"""Request and response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HistoryTurn(BaseModel):
    """One earlier turn. Assistant turns may include the SQL that ran."""

    role: Literal["user", "assistant"]
    content: str = ""
    sql: str | None = None


class ChatRequest(BaseModel):
    """A question plus a short slice of the browser conversation."""

    question: str = ""
    history: list[HistoryTurn] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """Every domain outcome uses this shape, including blocked questions."""

    status: str
    answer: str
    sql: str | None = None
    reasoning: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    chart: dict[str, Any] = Field(default_factory=dict)
    followups: list[str] = Field(default_factory=list)
    safety: dict[str, Any] = Field(default_factory=dict)
    block_reason: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    repairs: int = 0
    llm_calls: int = 0
    elapsed_ms: int = 0
    model: str = ""
