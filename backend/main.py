"""
checkout-api: Fictional e-commerce checkout backend.

This service intentionally supports three failure modes for educational use:
  - memory_leak:      Memory grows with each request; latency degrades over time.
  - code_bug:         30% of checkouts crash with a NullPointerException.
  - payment_outage:   Payment provider returns HTTP 503; retries do not help.

Trigger failures via POST /admin/set-failure  {"mode": "memory_leak"}
Reset to normal    via POST /admin/reset
Read live metrics  via GET  /metrics
"""

import json
import logging
import os
import random
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Structured JSON logger → writes to logs/app.log
# ---------------------------------------------------------------------------

os.makedirs("logs", exist_ok=True)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "checkout-api",
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _build_logger() -> logging.Logger:
    log = logging.getLogger("checkout")
    log.setLevel(logging.DEBUG)
    for handler in [logging.FileHandler("logs/app.log"), logging.StreamHandler()]:
        handler.setFormatter(JSONFormatter())
        log.addHandler(handler)
    return log


logger = _build_logger()


# ---------------------------------------------------------------------------
# Application state  (reset on container restart → intentional for memory_leak)
# ---------------------------------------------------------------------------

state: dict = {
    "failure_mode": "none",   # none | memory_leak | code_bug | payment_outage
    "memory_store": [],       # grows unbounded during memory_leak
    "request_count": 0,
    "error_count": 0,
    "start_time": time.time(),
}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Checkout API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    logger.info("Checkout service starting up")
    logger.info("Database connection pool initialized (5 connections)")
    logger.info("Payment provider client configured: https://pay.example.com")
    logger.info("Checkout service ready — listening on :8000")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CheckoutRequest(BaseModel):
    user_id: str
    cart_id: str
    amount: float


class FailureRequest(BaseModel):
    mode: str   # none | memory_leak | code_bug | payment_outage


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _db_get_user(user_id: str) -> dict | None:
    """Simulate a database user lookup.

    During the 'code_bug' failure mode, this returns None ~30% of the time,
    which causes a TypeError (NullPointerException) in the caller.
    """
    time.sleep(0.02)   # simulate DB round-trip

    if state["failure_mode"] == "code_bug":
        if random.random() < 0.30:
            logger.debug(f"DB query returned no rows for user_id={user_id!r}")
            return None   # <-- intentional: simulates missing DB record

    return {"id": user_id, "email": f"{user_id}@shopdemo.com", "name": "Demo User"}


def _call_payment_provider(amount: float) -> dict:
    """Simulate an HTTP call to an external payment provider.

    During 'payment_outage', the provider responds with 503.
    This error is not recoverable by restarting the service.
    """
    if state["failure_mode"] == "payment_outage":
        logger.warning(
            "Payment provider returned HTTP 503 Service Unavailable — "
            "upstream dependency is down"
        )
        logger.warning("Retrying payment request (attempt 2/3)...")
        time.sleep(0.5)
        logger.warning("Retrying payment request (attempt 3/3)...")
        time.sleep(0.5)
        logger.error(
            "All retry attempts exhausted. Payment provider unreachable. "
            "Manual escalation required."
        )
        raise HTTPException(
            status_code=502,
            detail="Payment provider unavailable (upstream HTTP 503). Escalate to payments team.",
        )

    time.sleep(0.05)   # simulate network latency
    return {
        "transaction_id": f"txn_{random.randint(10_000, 99_999)}",
        "status": "approved",
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok", "failure_mode": state["failure_mode"]}


@app.get("/metrics")
def metrics() -> dict:
    """Returns simulated operational metrics as JSON.

    Metrics are derived from current state:
    - memory_usage grows during memory_leak
    - latency grows during memory_leak
    - error_rate is calculated from real request/error counts
    """
    total = state["request_count"]
    errors = state["error_count"]

    # Memory: 64 MB baseline + 0.5 MB per leaked chunk
    memory_mb = round(64.0 + len(state["memory_store"]) * 0.5, 1)

    # Latency: 45 ms baseline + 100 ms per chunk (matches time.sleep(chunks * 0.1) in checkout)
    latency_ms = 45 + min(len(state["memory_store"]) * 100, 2_000)

    # DB connections: simulated active pool usage
    db_connections = min(5 + (total % 8), 20)

    return {
        "failure_mode": state["failure_mode"],
        "error_rate": round(errors / total, 3) if total > 0 else 0.0,
        "latency_ms": latency_ms,
        "memory_usage_mb": memory_mb,
        "db_connections": db_connections,
        "request_count": total,
        "error_count": errors,
        "uptime_seconds": round(time.time() - state["start_time"]),
    }


@app.post("/checkout")
def checkout(req: CheckoutRequest) -> dict:
    state["request_count"] += 1
    logger.info(
        f"Checkout initiated: user={req.user_id} cart={req.cart_id} amount=${req.amount:.2f}"
    )

    # ------------------------------------------------------------------
    # FAILURE MODE: memory_leak
    # Each request appends ~50 KB to an in-memory list that is never freed.
    # Latency increases proportionally. Restarting the container clears state.
    # ------------------------------------------------------------------
    if state["failure_mode"] == "memory_leak":
        chunk = "x" * 50_000   # ~50 KB per request
        state["memory_store"].append(chunk)
        allocated = len(state["memory_store"])
        delay = min(allocated * 0.1, 2.0)
        logger.warning(
            f"High memory pressure: {allocated} chunks allocated "
            f"({allocated * 0.05:.1f} MB above baseline). "
            f"Adding {delay:.2f}s artificial latency."
        )
        time.sleep(delay)

    # ------------------------------------------------------------------
    # Database lookup — may return None during code_bug failure mode
    # ------------------------------------------------------------------
    try:
        user = _db_get_user(req.user_id)

        # ROOT CAUSE OF code_bug FAILURE MODE (line below):
        # If _db_get_user() returns None, accessing user["email"] raises TypeError.
        # Fix: add  `if user is None: raise HTTPException(404, "User not found")`
        email = user["email"]   # TypeError: 'NoneType' is not subscriptable

        logger.info(f"User record retrieved: id={req.user_id} email={email}")

    except TypeError as exc:
        state["error_count"] += 1
        logger.error(
            f"NullPointerException: user object is None for user_id={req.user_id!r}. "
            f"Cannot read attribute 'email' on NoneType. "
            f"Traceback: {exc}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error: user object was null. See logs for stack trace.",
        )

    # ------------------------------------------------------------------
    # Payment provider call — always fails during payment_outage
    # ------------------------------------------------------------------
    try:
        payment = _call_payment_provider(req.amount)
        logger.info(f"Payment approved: transaction_id={payment['transaction_id']}")

    except HTTPException:
        state["error_count"] += 1
        logger.error(
            f"Checkout failed for cart={req.cart_id}: "
            "payment provider is unavailable. Customer charge was NOT processed."
        )
        raise

    logger.info(
        f"Checkout complete: user={req.user_id} "
        f"transaction={payment['transaction_id']} amount=${req.amount:.2f}"
    )
    return {
        "status": "success",
        "transaction_id": payment["transaction_id"],
        "user_id": req.user_id,
        "amount": req.amount,
    }


@app.post("/admin/set-failure")
def set_failure(req: FailureRequest) -> dict:
    """Inject a failure mode. Used by the incident generator and manual testing."""
    valid_modes = {"none", "memory_leak", "code_bug", "payment_outage"}
    if req.mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown mode {req.mode!r}. Valid modes: {sorted(valid_modes)}",
        )
    old = state["failure_mode"]
    state["failure_mode"] = req.mode

    if req.mode == "none":
        logger.info(f"Failure mode cleared (was: {old!r}). Service restored to normal.")
    else:
        logger.warning(
            f"INCIDENT INJECTED: failure_mode set to {req.mode!r} (was: {old!r})"
        )
    return {"failure_mode": state["failure_mode"]}


@app.post("/admin/reset")
def reset() -> dict:
    """Reset all state. Simulates a service restart (clears memory leak)."""
    state["failure_mode"] = "none"
    state["memory_store"].clear()
    state["request_count"] = 0
    state["error_count"] = 0
    state["start_time"] = time.time()
    logger.info("Service state fully reset. Memory cleared. Counters reset.")
    return {"status": "reset"}
