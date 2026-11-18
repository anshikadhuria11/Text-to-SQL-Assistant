"""Steps in the question workflow. The model proposes SQL; these steps decide."""

from __future__ import annotations

import contextvars
import time
from typing import Any

from app.charts import validate_chart
from app.config import settings
from app.db import QueryTimeout, execute_select, get_schema, reference_date
from app.llm import ExplainDraft, ModelBusy, SqlDraft, TableDraft
from app.prompts import explain_messages, repair_messages, sql_messages, table_messages
from app.safety import SafetyError, validate_sql
from app.schema_notes import build_schema_text

_MODEL = contextvars.ContextVar("store_model")

SAFE_ANSWER = "The figures are in the table. Instructions found inside the data were ignored."
BUSY_ANSWER = "The free model is busy, try again in a few seconds."
SCOPE_ANSWER = (
    "I can only answer questions about this store: products, orders, customers, reviews, and sales."
)
REPAIR_ANSWER = "I could not produce a safe query for that question. Try rephrasing it."
BANNED_ANSWER = ("drop table", "delete from", "ignore previous instructions")


def set_model(model: Any):
    """Bind the model used by the steps in this request."""
    return _MODEL.set(model)


def reset_model(token) -> None:
    """Restore the previous model binding."""
    _MODEL.reset(token)


def _model():
    return _MODEL.get()


def _step(name: str, status: str, detail: str, started: float) -> dict[str, Any]:
    return {
        "step": name,
        "status": status,
        "detail": detail,
        "ms": int((time.perf_counter() - started) * 1000),
    }


def _busy(step: str, started: float, calls: int) -> dict[str, Any]:
    return {
        "status": "error",
        "answer": BUSY_ANSWER,
        "llm_calls": calls,
        "trace": [_step(step, "error", "The free model is busy.", started)],
    }


def _complete(state: dict, schema: type, messages: list, temperature: float, step: str, started: float):
    calls = state.get("llm_calls", 0)
    if calls >= settings.max_llm_calls:
        return None, {
            "status": "error",
            "answer": REPAIR_ANSWER,
            "llm_calls": calls,
            "trace": [_step(step, "error", "Stopped after 6 model calls.", started)],
        }
    try:
        payload = _model().call(schema=schema, messages=messages, temperature=temperature)
    except ModelBusy:
        return None, _busy(step, started, calls)
    return payload, None


def _finish_intent(payload: dict, calls: int, step: str, started: float) -> dict[str, Any] | None:
    intent = payload.get("intent")
    if intent == "needs_clarification":
        question = payload.get("clarification_question") or "Could you say a bit more about what you want?"
        return {
            "status": "clarification",
            "intent": intent,
            "clarification": question,
            "answer": question,
            "reasoning": payload.get("reasoning"),
            "llm_calls": calls,
            "trace": [_step(step, "ok", "Asked a clarifying question.", started)],
        }
    if intent == "out_of_scope":
        return {
            "status": "out_of_scope",
            "intent": intent,
            "answer": SCOPE_ANSWER,
            "reasoning": payload.get("reasoning"),
            "llm_calls": calls,
            "trace": [_step(step, "ok", "Question is outside store data.", started)],
        }
    return None


def prepare(state: dict) -> dict:
    """Trim the question and attach a compact schema listing."""
    started = time.perf_counter()
    question = " ".join((state.get("question") or "").split())
    history = list(state.get("history") or [])[-settings.history_turns :]
    trimmed = []
    for item in history:
        content = (item.get("content") or "")[: settings.history_char_cap]
        trimmed.append(
            {"role": item.get("role", "user"), "content": content, "sql": item.get("sql")}
        )
    schema = get_schema()
    text = build_schema_text(schema["tables"], reference_date())
    detail = f"Schema text is {len(text)} characters across {len(schema['tables'])} tables."
    return {
        "question": question,
        "history": trimmed,
        "schema_text": text,
        "selected_tables": [table["name"] for table in schema["tables"]],
        "attempts": 0,
        "llm_calls": 0,
        "columns": [],
        "rows": [],
        "truncated": False,
        "followups": [],
        "chart": {"type": "none", "x": None, "y": [], "title": ""},
        "trace": [_step("prepare", "ok", detail, started)],
    }


def route_prepare(state: dict) -> str:
    """Use table selection only when the schema listing is over the budget."""
    if len(state.get("schema_text") or "") > settings.schema_token_budget_chars:
        return "pick_tables"
    return "write_sql"


def route_after_pick(state: dict) -> str:
    """Stop when table selection could not reach the model."""
    if state.get("status"):
        return "finish"
    return "write_sql"


def pick_tables(state: dict) -> dict:
    """Keep a subset of real table names. Unused for the bundled 7-table schema."""
    started = time.perf_counter()
    names = [table["name"] for table in get_schema()["tables"]]
    payload, early = _complete(
        state,
        TableDraft,
        table_messages(state["question"], names),
        0,
        "pick_tables",
        started,
    )
    if early:
        return early
    calls = state.get("llm_calls", 0) + 1
    chosen = [name for name in payload.get("tables") or [] if name in names]
    if not chosen:
        chosen = names
    text = build_schema_text(get_schema()["tables"], reference_date(), only=set(chosen))
    return {
        "selected_tables": chosen,
        "schema_text": text,
        "llm_calls": calls,
        "trace": [_step("pick_tables", "ok", "Using " + ", ".join(chosen) + ".", started)],
    }


def write_sql(state: dict) -> dict:
    """Ask for one structured SQL decision."""
    started = time.perf_counter()
    payload, early = _complete(
        state,
        SqlDraft,
        sql_messages(
            state["question"],
            state["schema_text"],
            state.get("history") or [],
            reference_date(),
        ),
        0,
        "write_sql",
        started,
    )
    if early:
        return early
    calls = state.get("llm_calls", 0) + 1
    finished = _finish_intent(payload, calls, "write_sql", started)
    if finished:
        return finished
    sql = payload.get("sql")
    return {
        "intent": payload.get("intent") or "answerable",
        "sql": sql,
        "reasoning": payload.get("reasoning"),
        "error": None,
        "llm_calls": calls,
        "trace": [_step("write_sql", "ok", "Drafted a SELECT.", started)],
    }


def route_after_write(state: dict) -> str:
    """Stop early for clarification and out-of-scope questions."""
    if state.get("status") in {"clarification", "out_of_scope", "error"}:
        return "finish"
    return "check"


def check(state: dict) -> dict:
    """Run the safety layer. Destructive statements are not sent back for repair."""
    started = time.perf_counter()
    try:
        validated, note = validate_sql(
            state.get("sql") or "",
            get_schema()["mapping"],
            max_rows=settings.max_rows,
        )
    except SafetyError as exc:
        if exc.destructive:
            return {
                "status": "blocked",
                "block_reason": exc.reason,
                "error": exc.reason,
                "trace": [_step("check", "blocked", exc.reason, started)],
            }
        return {
            "error": exc.reason,
            "validated_sql": None,
            "trace": [_step("check", "error", exc.reason, started)],
        }
    return {
        "validated_sql": validated,
        "error": None,
        "limit_note": note,
        "block_reason": None,
        "trace": [_step("check", "ok", f"Parsed 1 statement, SELECT. {note}.", started)],
    }


def route_after_check(state: dict) -> str:
    """Execute a clean statement, stop a blocked one, or repair the rest."""
    if state.get("status") == "blocked":
        return "finish"
    if state.get("error"):
        return "repair"
    return "execute"


def execute(state: dict) -> dict:
    """Run the rewritten statement on the read-only connection."""
    started = time.perf_counter()
    try:
        result = execute_select(state["validated_sql"])
    except QueryTimeout as exc:
        return {
            "error": str(exc),
            "trace": [_step("execute", "error", str(exc), started)],
        }
    except Exception:
        return {
            "error": "The query failed.",
            "trace": [_step("execute", "error", "The query failed.", started)],
        }
    rows = result["rows"]
    detail = f"Returned {len(rows)} rows in {result['elapsed_ms']} ms."
    if not rows:
        detail = "Returned 0 rows. Nothing matched."
    return {
        "columns": result["columns"],
        "rows": rows,
        "truncated": result["truncated"],
        "error": None,
        "trace": [_step("execute", "ok", detail, started)],
    }


def route_after_execute(state: dict) -> str:
    """Explain a finished result, including an empty one."""
    if state.get("error"):
        return "repair"
    return "explain"


def repair(state: dict) -> dict:
    """Ask for a new SELECT. Stop after the configured number of tries."""
    started = time.perf_counter()
    done = state.get("attempts", 0)
    calls = state.get("llm_calls", 0)
    if done >= settings.max_repairs or calls >= settings.max_llm_calls:
        return {
            "status": "error",
            "answer": REPAIR_ANSWER,
            "attempts": done,
            "trace": [_step("repair", "error", "Stopped after the repair limit.", started)],
        }
    payload, early = _complete(
        state,
        SqlDraft,
        repair_messages(
            state["question"],
            state["schema_text"],
            state.get("sql") or "",
            state.get("error") or "The statement was rejected.",
            reference_date(),
        ),
        0,
        "repair",
        started,
    )
    if early:
        early["attempts"] = done
        return early
    calls += 1
    done += 1
    finished = _finish_intent(payload, calls, "repair", started)
    if finished:
        finished["attempts"] = done
        return finished
    return {
        "sql": payload.get("sql"),
        "reasoning": payload.get("reasoning") or state.get("reasoning"),
        "intent": payload.get("intent") or "answerable",
        "attempts": done,
        "error": None,
        "llm_calls": calls,
        "trace": [_step("repair", "ok", f"Repair attempt {done}.", started)],
    }


def route_after_repair(state: dict) -> str:
    """Re-check a replacement statement, or stop when the step already finished."""
    if state.get("status") in {"error", "clarification", "out_of_scope", "blocked"}:
        return "finish"
    return "check"


def _filter_answer(answer: str) -> str:
    lowered = answer.lower()
    if any(term in lowered for term in BANNED_ANSWER):
        return SAFE_ANSWER
    return answer


def explain(state: dict) -> dict:
    """Turn rows into a short answer and a checked chart spec."""
    started = time.perf_counter()
    columns = state.get("columns") or []
    rows = state.get("rows") or []
    payload, early = _complete(
        state,
        ExplainDraft,
        explain_messages(state["question"], state.get("validated_sql") or "", columns, rows),
        0.2,
        "explain",
        started,
    )
    if early:
        return early
    calls = state.get("llm_calls", 0) + 1
    answer = _filter_answer((payload.get("answer") or "").strip())
    if not rows:
        answer = "Nothing matched that question."
    chart = validate_chart(payload.get("chart"), columns, rows)
    if not rows:
        chart = {"type": "none", "x": None, "y": [], "title": ""}
    followups = [str(item) for item in (payload.get("followups") or []) if str(item).strip()]
    while len(followups) < 2:
        followups.append("Show this broken down by region")
    return {
        "status": "ok",
        "answer": answer,
        "chart": chart,
        "followups": followups[:2],
        "llm_calls": calls,
        "trace": [_step("explain", "ok", "Wrote a short answer.", started)],
    }


def finish(state: dict) -> dict:
    """Mark the end of the workflow. The response is assembled by the caller."""
    started = time.perf_counter()
    status = state.get("status") or "error"
    if status == "ok":
        detail = "Assembled the answer."
    elif status == "blocked":
        detail = state.get("block_reason") or "Blocked."
    else:
        detail = status
    return {"trace": [_step("finish", status, detail, started)]}
