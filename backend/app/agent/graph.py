"""LangGraph assembly and the single entry point used by the API and the evaluation."""

from __future__ import annotations

import time
from typing import Any

from langgraph.graph import END, StateGraph

from app.agent.nodes import (
    check,
    execute,
    explain,
    finish,
    pick_tables,
    prepare,
    repair,
    reset_model,
    route_after_check,
    route_after_execute,
    route_after_repair,
    route_after_write,
    route_after_pick,
    route_prepare,
    set_model,
    write_sql,
)
from app.agent.state import AgentState
from app.config import settings
from app.llm import GroqModel, ModelBusy

_GRAPH = None


def build_graph():
    """Connect the steps. Destructive failures never return to repair."""
    graph = StateGraph(AgentState)
    graph.add_node("prepare", prepare)
    graph.add_node("pick_tables", pick_tables)
    graph.add_node("write_sql", write_sql)
    graph.add_node("check", check)
    graph.add_node("execute", execute)
    graph.add_node("repair", repair)
    graph.add_node("explain", explain)
    graph.add_node("finish", finish)
    graph.set_entry_point("prepare")
    graph.add_conditional_edges("prepare", route_prepare, ["pick_tables", "write_sql"])
    graph.add_conditional_edges("pick_tables", route_after_pick, ["write_sql", "finish"])
    graph.add_conditional_edges("write_sql", route_after_write, ["check", "finish"])
    graph.add_conditional_edges("check", route_after_check, ["execute", "repair", "finish"])
    graph.add_conditional_edges("execute", route_after_execute, ["explain", "repair"])
    graph.add_conditional_edges("repair", route_after_repair, ["check", "finish"])
    graph.add_edge("explain", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


def get_graph():
    """Compile once per process."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def _response(state: dict[str, Any], started: float, model_name: str) -> dict[str, Any]:
    status = state.get("status") or "error"
    rows = state.get("rows") or []
    validated = state.get("validated_sql")
    if status == "blocked":
        sql_out = state.get("sql")
    elif status == "ok":
        sql_out = validated
    else:
        sql_out = None
    verified = bool(validated) and status == "ok"
    return {
        "status": status,
        "answer": state.get("answer") or "",
        "sql": sql_out,
        "reasoning": state.get("reasoning"),
        "columns": state.get("columns") or [],
        "rows": rows,
        "row_count": len(rows),
        "truncated": bool(state.get("truncated")),
        "chart": state.get("chart") or {"type": "none", "x": None, "y": [], "title": ""},
        "followups": state.get("followups") or [],
        "safety": {
            "read_only": True,
            "single_select": bool(validated) and status != "blocked",
            "schema_verified": verified,
            "limit_enforced": bool(validated) and status != "blocked",
            "timeout_s": settings.query_timeout_s,
        },
        "block_reason": state.get("block_reason"),
        "trace": state.get("trace") or [],
        "repairs": state.get("attempts") or 0,
        "llm_calls": state.get("llm_calls") or 0,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "model": model_name,
    }


def run_agent(question: str, history: list | None = None, llm: Any | None = None) -> dict[str, Any]:
    """Answer one question. Pass `llm` to avoid the network in tests."""
    started = time.perf_counter()
    try:
        model = llm or GroqModel()
    except ModelBusy:
        return _response(
            {
                "status": "error",
                "answer": "The free model is busy, try again in a few seconds.",
                "trace": [],
            },
            started,
            settings.groq_model,
        )
    token = set_model(model)
    try:
        final = get_graph().invoke(
            {
                "question": question,
                "history": history or [],
                "trace": [],
                "llm_calls": 0,
                "attempts": 0,
            }
        )
    finally:
        reset_model(token)
    return _response(final, started, getattr(model, "model_name", settings.groq_model))
