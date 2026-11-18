"""Model caller with a short backoff and one fallback model."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from app.config import settings


class ChartDraft(BaseModel):
    """Chart fields requested by the model."""

    type: str = "none"
    x: str | None = None
    y: list[str] = Field(default_factory=list)
    title: str = ""


class SqlDraft(BaseModel):
    """Structured SQL decision."""

    intent: str
    clarification_question: str | None = None
    reasoning: str = ""
    sql: str | None = None


class TableDraft(BaseModel):
    """Tables chosen for a question."""

    tables: list[str] = Field(default_factory=list)


class ExplainDraft(BaseModel):
    """Answer, chart, and follow-up questions."""

    answer: str
    chart: ChartDraft = Field(default_factory=ChartDraft)
    followups: list[str] = Field(default_factory=list)


class ModelBusy(Exception):
    """The free model could not answer after retries."""


class ScriptedModel:
    """Returns canned structured results. Tests use this instead of the network."""

    def __init__(self, outputs: list[Any], model_name: str = "scripted") -> None:
        self.outputs = list(outputs)
        self.model_name = model_name
        self.calls = 0

    def call(self, *, schema: type[BaseModel], messages: list, temperature: float) -> dict:
        self.calls += 1
        if not self.outputs:
            raise RuntimeError("No scripted outputs left.")
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, BaseModel):
            return item.model_dump()
        return dict(item)


def _rate_limited(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status == 429 or "rate limit" in text or "too many requests" in text


class GroqModel:
    """ChatGroq wrapper. Retries rate limits, then tries the fallback model once."""

    def __init__(self) -> None:
        if not settings.groq_api_key:
            raise ModelBusy("Missing GROQ_API_KEY.")
        from langchain_groq import ChatGroq

        self.model_name = settings.groq_model
        self._primary = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            temperature=0,
        )
        self._fallback = ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.groq_fallback_model,
            temperature=0,
        )
        self._used_fallback = False

    def call(self, *, schema: type[BaseModel], messages: list, temperature: float) -> dict:
        delays = (0.4, 0.8)
        attempt = 0
        while True:
            client = self._fallback if self._used_fallback else self._primary
            try:
                runnable = client.with_structured_output(schema, method="function_calling")
                result = runnable.invoke(messages, temperature=temperature)
                if isinstance(result, BaseModel):
                    return result.model_dump()
                return dict(result)
            except Exception as exc:
                if not _rate_limited(exc):
                    raise
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                    attempt += 1
                    continue
                if not self._used_fallback:
                    self._used_fallback = True
                    self.model_name = settings.groq_fallback_model
                    continue
                raise ModelBusy("rate limit") from exc
