"""HTTP API. Serves the built UI when frontend/dist is present."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent.graph import run_agent
from app.config import REPO_ROOT, settings
from app.db import data_hash, get_schema, prepare, reference_date
from app.ratelimit import RateLimiter
from app.schema_notes import BUSINESS_RULES, TABLE_DESCRIPTIONS
from app.schemas import ChatRequest, ChatResponse

logger = logging.getLogger("store")
logging.basicConfig(level=logging.INFO)

limiter = RateLimiter(settings.rate_limit_per_min)
DIST = REPO_ROOT / "frontend" / "dist"

app = FastAPI(title="Ask Your Store", docs_url="/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    prepare()


@app.middleware("http")
async def _guard(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception:
        logger.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Something went wrong. Please try again."},
        )


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client:
        return request.client.host
    return "unknown"


@app.get("/api/health")
def health() -> dict:
    """Report the model name, the store date, and a short data hash."""
    db_ok = True
    try:
        get_schema()
    except Exception:
        logger.exception("Database check failed")
        db_ok = False
    return {
        "status": "ok" if db_ok else "error",
        "model": settings.groq_model,
        "reference_date": reference_date() if db_ok else None,
        "db_ok": db_ok,
        "data_hash": data_hash() if db_ok else None,
    }


@app.get("/api/schema")
def schema() -> dict:
    """Tables, columns, row counts, and the business-rule summary for the sidebar."""
    payload = get_schema()
    tables = []
    for table in payload["tables"]:
        tables.append(
            {
                **table,
                "description": TABLE_DESCRIPTIONS.get(table["name"], ""),
            }
        )
    return {
        "reference_date": reference_date(),
        "tables": tables,
        "rules": list(BUSINESS_RULES[:3]),
    }


@app.post("/api/chat", response_model=ChatResponse)
def chat(body: ChatRequest, request: Request):
    """Answer one question. Domain outcomes are HTTP 200; the body carries status."""
    question = body.question.strip()
    if not question:
        return JSONResponse(status_code=422, content={"detail": "Please enter a question."})
    if len(question) > settings.max_question_chars:
        return JSONResponse(
            status_code=422,
            content={"detail": f"Please keep questions under {settings.max_question_chars} characters."},
        )
    allowed, retry_after = limiter.allow(_client_key(request))
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many questions, wait a few seconds."},
            headers={"Retry-After": str(retry_after)},
        )
    history = []
    for turn in body.history[-settings.history_turns :]:
        history.append(
            {
                "role": turn.role,
                "content": turn.content[: settings.history_char_cap],
                "sql": turn.sql,
            }
        )
    try:
        result = run_agent(question, history)
    except Exception:
        logger.exception("Chat failed")
        result = {
            "status": "error",
            "answer": "Something went wrong. Please try again.",
            "sql": None,
            "reasoning": None,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "chart": {"type": "none", "x": None, "y": [], "title": ""},
            "followups": [],
            "safety": {
                "read_only": True,
                "single_select": False,
                "schema_verified": False,
                "limit_enforced": False,
                "timeout_s": settings.query_timeout_s,
            },
            "block_reason": None,
            "trace": [],
            "repairs": 0,
            "llm_calls": 0,
            "elapsed_ms": 0,
            "model": settings.groq_model,
        }
    return result


def _mount_ui(application: FastAPI) -> None:
    if not DIST.exists():
        return
    assets = DIST / "assets"
    if assets.exists():
        application.mount("/assets", StaticFiles(directory=assets), name="assets")

    @application.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        candidate = (DIST / full_path).resolve()
        if full_path and candidate.is_file() and DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")


_mount_ui(app)
