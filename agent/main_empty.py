"""
Incident Investigation Agent — automated monitoring + multi-provider

Model B: background monitor polls /metrics every 30 s and auto-triggers
investigations when a failure mode is detected.

Endpoints
---------
GET  /                                            → web UI
GET  /providers                                   → which providers are configured
GET  /stream                                      → SSE: all monitoring + investigation events
POST /investigate                                 → trigger manual investigation
POST /investigate/{session_id}/approve/{id}       → approve a mutating tool call
POST /investigate/{session_id}/deny/{id}          → deny a mutating tool call
"""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncGenerator

import logging

import anthropic
import httpx

logger = logging.getLogger("agent")
from openai import AsyncOpenAI
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BACKEND_URL   = os.getenv("BACKEND_URL",       "http://localhost:8000")
LOG_FILE      = os.getenv("LOG_FILE",          "/logs/app.log")
SOURCE_FILE   = os.getenv("SOURCE_FILE",       "/source/main.py")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY",    "")
GOOGLE_KEY    = os.getenv("GOOGLE_API_KEY",    "")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

sessions:              dict[str, dict]     = {}
monitor_subscribers:   list[asyncio.Queue] = []
active_investigations: dict[str, str]      = {}  # anomaly label → session_id | "pending"
investigated_anomalies: set[str]           = set()  # anomalies already investigated this incident
last_metrics:          dict                = {}

MUTATING_TOOLS = {"reset_service"}

# ---------------------------------------------------------------------------
# Anomaly detection thresholds
# ---------------------------------------------------------------------------

ANOMALY_THRESHOLDS = {
    "high_error_rate": lambda m: float(m.get("error_rate", 0)) > 0.10,
    "high_latency":    lambda m: float(m.get("latency_ms", 0)) > 300,
    "high_memory":     lambda m: float(m.get("memory_usage_mb", 0)) > 150,
}

# ---------------------------------------------------------------------------
# API clients
# ---------------------------------------------------------------------------

anthropic_async = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None
openai_client   = AsyncOpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None
gemini_client   = AsyncOpenAI(
    api_key=GOOGLE_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
) if GOOGLE_KEY else None

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def read_source_file() -> str:
    # TODO: Finish the read_source_file function tool 
    # by opening the SOURCE_FILE and returning its 
    # contents as a string. 
    # HINTS: 
    #   1. Use a try-except block to handle file read errors gracefully. If the file is not found, return a string indicating the issue.
    #   2. Return f_stream.read() after opening the file using a 'with' statement.
    pass

async def get_metrics() -> str:
    async with httpx.AsyncClient() as http:
        r = await http.get(f"{BACKEND_URL}/metrics", timeout=5.0)
        data = r.json()
        data.pop("failure_mode", None)  # agent must diagnose from metrics, not labels
        return json.dumps(data, indent=2)

async def search_logs(query: str, level: str | None = None) -> str:
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        matches = [l for l in lines if query.lower() in l.lower()]
        if level:
            matches = [l for l in matches if f'"level": "{level.upper()}"' in l]
        return "".join(matches[-20:]) or "No matches found."
    except FileNotFoundError:
        return f"Log file not found at {LOG_FILE}"

async def get_recent_logs(n: int = 25) -> str:
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except FileNotFoundError:
        return f"Log file not found at {LOG_FILE}"

async def reset_service() -> str:
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{BACKEND_URL}/admin/reset", timeout=5.0)
        return json.dumps(r.json(), indent=2)
    
async def get_health() -> str:
    async with httpx.AsyncClient() as http:
        r = await http.get(f"{BACKEND_URL}/health", timeout=5.0)
        return json.dumps(r.json(), indent=2)



TOOL_HANDLERS = {
    "get_metrics":      get_metrics,
    "get_health":       get_health,
    "search_logs":      search_logs,
    "get_recent_logs":  get_recent_logs,
    "read_source_file": read_source_file,
    "reset_service":    reset_service,
}

# ---------------------------------------------------------------------------
# Tool schema definitions
# ---------------------------------------------------------------------------

_TOOL_DEFS = [
    {
        "name": "get_metrics",
        "description": (
            "Fetch live service metrics: error_rate, latency_ms, memory_usage_mb, "
            "db_connections, request_count, error_count, uptime_seconds."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_health",
        "description": "Fetch service health status and the currently active failure mode.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_logs",
        "description": (
            "Search application logs for a keyword. "
            "Optionally filter by log level. Returns up to 30 matching lines."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term to find in log messages"},
                "level": {
                    "type": "string",
                    "enum": ["ERROR", "WARNING", "INFO"],
                    "description": "Optional log level filter",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_recent_logs",
        "description": "Get the most recent N log entries from the application log.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of recent log lines (default 50)"},
            },
        },
    },
    # TODO: Write the tool schema for read_source_file.
    # The schema tells the LLM that this tool exists and what it does.
    # Without it, the agent cannot call the tool — even if the function exists.
    #
    # HINTS:
    # 1. Follow the same structure as the other schemas above (name, description, input_schema).
    # 2. The description should tell the LLM *when* to use this tool
    #    (e.g. "Read the backend source code to inspect the implementation and identify the root cause of code bugs.").
    # 3. This tool takes no parameters, so input_schema is: {"type": "object", "properties": {}}

    {
        "name": "reset_service",
        "description": (
            "Reset all service state: clears the memory leak, counters, and failure mode. "
            "Simulates a service restart. Only useful for memory leak or runtime state issues. "
            "REQUIRES HUMAN APPROVAL. Do NOT propose for code bugs — restart won't fix code errors."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

CLAUDE_TOOLS = _TOOL_DEFS

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in _TOOL_DEFS
]

SYSTEM_PROMPT = """\
You are an expert on-call incident investigation assistant for a checkout service.

You will receive an alert triggered by anomalous metrics. Your job is to determine
the root cause. You are NOT told what is wrong — you must figure it out.

# TODO: Write the investigation strategy and tool safety rules.
# The system prompt is how you program the agent's behavior — it replaces
# if/else logic in code with natural language instructions.
#
# HINTS — your prompt should cover:
# 1. Investigation strategy: What order should the agent use its tools?
#    (e.g. check metrics first, then logs, then source code if needed)
# 2. Tool safety rules: Which tools are safe to call freely?
#    Which tools are dangerous and need human approval? (reset_service)
# 3. When should the agent read source code? (e.g. when it finds exceptions in logs)

When you find exceptions or tracebacks in the logs, ALWAYS read the source code to find the
exact line causing the error. If the root cause is a code defect, include a specific fix:

**Code Fix Required**:
File: `main.py`, Line [N]
```diff
- [the current buggy line]
+ [the corrected line]
```

End every investigation with a structured report:

## Incident Report

**Symptoms**: [what the metrics and logs show]
**Root Cause**: [why — include file/line references if found]
**Impact**: [what is affected and severity level]
**Recommended Actions**:
1. [step one]
2. [step two]
...
**Will a Restart Help?**: [yes/no and why]
"""


# ---------------------------------------------------------------------------
# Provider session abstraction
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    id:    str
    name:  str
    input: dict


@dataclass
class TurnResult:
    thinking:   str | None
    text:       str | None
    tool_calls: list[ToolCall]
    done:       bool


class ClaudeSession:
    """Maintains Claude-format message history and calls the Anthropic API."""

    def __init__(self):
        self.messages: list = []

    def add_user_text(self, text: str):
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[dict]):
        self.messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
                for r in results
            ],
        })

    async def complete(self) -> TurnResult:
        response = await anthropic_async.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=CLAUDE_TOOLS,
            messages=self.messages,
        )
        # Store full content objects to preserve thinking block signatures
        self.messages.append({"role": "assistant", "content": response.content})

        thinking = next((b.thinking for b in response.content if b.type == "thinking"), None)
        text     = next((b.text     for b in response.content if b.type == "text"),     None)
        calls    = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in response.content if b.type == "tool_use"
        ]
        return TurnResult(
            thinking=thinking,
            text=text,
            tool_calls=calls,
            done=(response.stop_reason == "end_turn"),
        )


class OpenAISession:
    """Maintains OpenAI-format message history. Works for both OpenAI and Gemini."""

    def __init__(self, client: AsyncOpenAI, model: str):
        self.client   = client
        self.model    = model
        self.messages: list = []

    def add_user_text(self, text: str):
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[dict]):
        for r in results:
            self.messages.append({
                "role":         "tool",
                "tool_call_id": r["id"],
                "content":      r["content"],
            })

    async def complete(self) -> TurnResult:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + self.messages
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            tools=OPENAI_TOOLS,
            messages=msgs,
        )
        choice = response.choices[0]
        msg    = choice.message

        asst: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            asst["tool_calls"] = [
                {
                    "id":       tc.id,
                    "type":     "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        self.messages.append(asst)

        calls = []
        for tc in msg.tool_calls or []:
            try:
                inp = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                inp = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, input=inp))

        return TurnResult(
            thinking=None,
            text=msg.content or None,
            tool_calls=calls,
            done=(choice.finish_reason != "tool_calls"),
        )


# ---------------------------------------------------------------------------
# SSE helper + broadcast
# ---------------------------------------------------------------------------

def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


async def broadcast(event: dict):
    """Push an event to all connected /stream subscribers."""
    dead = []
    for q in monitor_subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            monitor_subscribers.remove(q)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Core agentic loop (yields event dicts)
# ---------------------------------------------------------------------------

async def agent_events(
    session_id: str,
    alert: str,
    provider: str,
) -> AsyncGenerator[dict, None]:
    """Run the observe→plan→act loop. Yields plain event dicts (not SSE-formatted)."""
    store = sessions[session_id]

    if provider == "claude":
        if not anthropic_async:
            yield {"type": "error", "content": "ANTHROPIC_API_KEY is not configured."}
            yield {"type": "done"}
            return
        prov        = ClaudeSession()
        model_label = "Claude Haiku 4.5"

    elif provider == "openai":
        if not openai_client:
            yield {"type": "error", "content": "OPENAI_API_KEY is not configured."}
            yield {"type": "done"}
            return
        prov        = OpenAISession(openai_client, "gpt-4o-mini")
        model_label = "GPT-4o Mini"

    elif provider == "gemini":
        if not gemini_client:
            yield {"type": "error", "content": "GOOGLE_API_KEY is not configured."}
            yield {"type": "done"}
            return
        prov        = OpenAISession(gemini_client, "gemini-2.0-flash-lite")
        model_label = "Gemini 2.0 Flash Lite"

    else:
        yield {"type": "error", "content": f"Unknown provider: {provider!r}"}
        yield {"type": "done"}
        return

    yield {"type": "provider", "label": model_label}
    yield {"type": "status",   "content": "Starting investigation…"}

    prov.add_user_text(alert)

    for step in range(1, 11):
        yield {"type": "status", "content": f"Reasoning… (step {step})"}

        try:
            result = await prov.complete()
        except Exception as exc:
            yield {"type": "error", "content": f"API error: {exc}"}
            break

        if result.thinking:
            preview = result.thinking[:400] + ("…" if len(result.thinking) > 400 else "")
            yield {"type": "thinking", "content": preview}

        if result.done:
            if result.text:
                yield {"type": "report", "content": result.text}
            break

        tool_results = []
        for tc in result.tool_calls:
            yield {"type": "tool_call", "name": tc.name, "input": tc.input}

            if tc.name in MUTATING_TOOLS:
                approval_event = asyncio.Event()
                store["pending_approvals"][tc.id] = {"event": approval_event, "approved": False}

                yield {"type": "approval_needed", "tool_use_id": tc.id, "name": tc.name}

                try:
                    await asyncio.wait_for(approval_event.wait(), timeout=120.0)
                    approved = store["pending_approvals"][tc.id]["approved"]
                except asyncio.TimeoutError:
                    approved = False

                yield {"type": "approval_result", "approved": approved, "name": tc.name,
                       "tool_use_id": tc.id}
                result_str = (
                    await TOOL_HANDLERS[tc.name]()
                    if approved
                    else "Action denied by operator. Skipping this remediation step."
                )
            else:
                result_str = await TOOL_HANDLERS[tc.name](**tc.input)

            preview = result_str[:1200] + ("…" if len(result_str) > 1200 else "")
            yield {"type": "tool_result", "name": tc.name, "content": preview}
            tool_results.append({"id": tc.id, "content": result_str})

        prov.add_tool_results(tool_results)
    else:
        yield {"type": "error", "content": "Max investigation steps reached."}

    yield {"type": "done"}
    sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# Monitored investigation (runs in background, broadcasts to /stream)
# ---------------------------------------------------------------------------

async def run_monitored_investigation(anomaly: str, alert: str, provider: str):
    session_id = str(uuid.uuid4())
    sessions[session_id] = {"pending_approvals": {}}
    active_investigations[anomaly] = session_id

    base = {"session_id": session_id, "triggered_by": "monitor", "anomaly": anomaly}
    await broadcast({**base, "type": "investigation_start", "alert": alert})

    try:
        async for event in agent_events(session_id, alert, provider):
            await broadcast({**base, **event})
    except Exception as exc:
        await broadcast({**base, "type": "error", "content": str(exc)})
    finally:
        active_investigations.pop(anomaly, None)
        investigated_anomalies.add(anomaly)   # don't re-investigate until metrics recover
        await broadcast({**base, "type": "investigation_end"})


# ---------------------------------------------------------------------------
# Alert builder
# ---------------------------------------------------------------------------

def _build_alert(anomaly: str, metrics: dict) -> str:
    """Build an alert message from raw metrics — no diagnosis, just the numbers."""
    error_pct = int(float(metrics.get("error_rate", 0)) * 100)
    mem_mb    = float(metrics.get("memory_usage_mb", 0))
    latency   = float(metrics.get("latency_ms", 0))
    req_count = int(metrics.get("request_count", 0))
    err_count = int(metrics.get("error_count", 0))

    header = "ALERT [AUTO]: Service anomaly detected by monitoring.\n"
    stats  = (
        f"Current metrics — error_rate: {error_pct}%, latency: {latency:.0f} ms, "
        f"memory: {mem_mb:.0f} MB, requests: {req_count}, errors: {err_count}."
    )

    if anomaly == "high_error_rate":
        trigger = f"Triggered by: error rate ({error_pct}%) exceeded threshold (>10%)."
    elif anomaly == "high_latency":
        trigger = f"Triggered by: latency ({latency:.0f} ms) exceeded threshold (>300 ms)."
    elif anomaly == "high_memory":
        trigger = f"Triggered by: memory usage ({mem_mb:.0f} MB) exceeded threshold (>150 MB)."
    else:
        trigger = f"Triggered by: {anomaly}."

    return (
        f"{header}{stats}\n{trigger}\n"
        "Investigate the root cause using the available tools. "
        "Check health, search logs, and read source code as needed."
    )


# ---------------------------------------------------------------------------
# Background monitoring loop
# ---------------------------------------------------------------------------

async def monitor_loop():
    """Poll backend metrics every 30 s. Auto-trigger investigation on anomaly detection."""
    await asyncio.sleep(10)  # give backend time to be healthy

    while True:
        try:
            raw     = await get_metrics()
            metrics = json.loads(raw)
            last_metrics.update(metrics)

            await broadcast({"type": "metrics_update", "data": dict(metrics)})

            # Detect which anomalies are currently firing
            firing = {
                label for label, check in ANOMALY_THRESHOLDS.items()
                if check(metrics)
            }

            if not firing:
                # All metrics healthy — reset so new anomalies are picked up fresh
                if investigated_anomalies:
                    investigated_anomalies.clear()
                    await broadcast({"type": "service_recovered"})
            else:
                # Investigate the first new anomaly not already handled
                for anomaly in firing:
                    if anomaly in investigated_anomalies:
                        logger.debug("Skipping %r — already investigated", anomaly)
                        continue
                    if anomaly in active_investigations:
                        logger.debug("Skipping %r — investigation in progress", anomaly)
                        continue

                    provider = (
                        "claude" if ANTHROPIC_KEY else
                        "openai" if OPENAI_KEY else
                        "gemini" if GOOGLE_KEY else None
                    )
                    if not provider:
                        logger.warning("Anomaly %r detected but no API keys configured", anomaly)
                        continue

                    active_investigations[anomaly] = "pending"
                    alert = _build_alert(anomaly, metrics)
                    await broadcast({
                        "type":    "alert_triggered",
                        "anomaly": anomaly,
                        "alert":   alert,
                    })
                    asyncio.create_task(
                        run_monitored_investigation(anomaly, alert, provider)
                    )

        except Exception as exc:
            logger.exception("Monitor loop error: %s", exc)

        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# App lifespan (starts monitor on startup)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(monitor_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Incident Investigation Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class InvestigateRequest(BaseModel):
    alert:    str
    provider: str = "claude"


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    ui_path = os.path.join(os.path.dirname(__file__), "ui", "index.html")
    with open(ui_path) as f:
        return f.read()


@app.get("/providers")
async def get_providers():
    return {
        "claude": {"label": "Claude Haiku 4.5",      "model": "claude-haiku-4-5-20251001", "available": bool(ANTHROPIC_KEY)},
        "openai": {"label": "GPT-4o Mini",            "model": "gpt-4o-mini",              "available": bool(OPENAI_KEY)},
        "gemini": {"label": "Gemini 2.0 Flash Lite",  "model": "gemini-2.0-flash-lite",    "available": bool(GOOGLE_KEY)},
    }


@app.get("/stream")
async def stream_events(request: Request):
    """Persistent SSE stream delivering monitoring metrics and all investigation events."""
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    monitor_subscribers.append(q)

    async def generate():
        try:
            # Immediately send current metrics snapshot so the UI isn't blank
            if last_metrics:
                yield sse({"type": "metrics_update", "data": dict(last_metrics)})

            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield sse(event)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # SSE comment keeps connection alive
        finally:
            try:
                monitor_subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


@app.post("/investigate")
async def investigate(req: InvestigateRequest):
    """Start a manual investigation. Events are broadcast to GET /stream."""
    session_id = str(uuid.uuid4())
    sessions[session_id] = {"pending_approvals": {}}

    base = {"session_id": session_id, "triggered_by": "manual"}

    async def run():
        await broadcast({**base, "type": "investigation_start", "alert": req.alert})
        try:
            async for event in agent_events(session_id, req.alert, req.provider):
                await broadcast({**base, **event})
        except Exception as exc:
            await broadcast({**base, "type": "error", "content": str(exc)})
        finally:
            await broadcast({**base, "type": "investigation_end"})

    asyncio.create_task(run())
    return {"session_id": session_id}


@app.post("/actions/reset")
async def action_reset():
    """Directly reset the service. Called from the UI 'Restart Service' button."""
    result = await reset_service()
    await broadcast({"type": "service_reset", "result": result})
    return {"ok": True, "result": result}


@app.post("/investigate/{session_id}/approve/{tool_use_id}")
async def approve_tool(session_id: str, tool_use_id: str):
    return await _resolve(session_id, tool_use_id, approved=True)


@app.post("/investigate/{session_id}/deny/{tool_use_id}")
async def deny_tool(session_id: str, tool_use_id: str):
    return await _resolve(session_id, tool_use_id, approved=False)


async def _resolve(session_id: str, tool_use_id: str, approved: bool) -> dict:
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    pending = session["pending_approvals"].get(tool_use_id)
    if not pending:
        raise HTTPException(status_code=404, detail="No pending approval for this tool call")
    pending["approved"] = approved
    pending["event"].set()
    return {"ok": True, "approved": approved}
