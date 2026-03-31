# ShopDemo — Incident Investigation Sandbox

A tiny fictional e-commerce checkout service that intentionally produces
operational incidents. Used as a target environment in an **AI agent tutorial**
for agentic incident investigation.

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│  Docker Compose Network                                    │
│                                                            │
│  ┌─────────────┐     POST /checkout     ┌──────────────┐  │
│  │  frontend   │ ──────────────────────▶│   backend    │  │
│  │  (nginx)    │                        │  (FastAPI)   │  │
│  │  :3000      │ ◀── JSON response ──── │  :8000       │  │
│  └─────────────┘                        │              │  │
│                                         │  /metrics    │  │
│  ┌──────────────────┐  POST /admin/     │  /checkout   │  │
│  │ incident-        │  set-failure ────▶│  /health     │  │
│  │ generator        │                   └──────────────┘  │
│  │ (Python loop)    │                         │            │
│  └──────────────────┘               logs/app.log          │
│                                     (volume-mounted)       │
└────────────────────────────────────────────────────────────┘
```

| Service            | URL                         | Purpose                          |
|--------------------|-----------------------------|----------------------------------|
| Frontend           | http://localhost:3000       | Checkout UI (trigger requests)   |
| Backend API        | http://localhost:8000       | FastAPI checkout service         |
| Metrics endpoint   | http://localhost:8000/metrics | Live JSON metrics              |
| API docs           | http://localhost:8000/docs  | Auto-generated Swagger UI        |
| App logs           | `./backend/logs/app.log`    | Structured JSON logs (host path) |

---

## Quick Start

```bash
# 1. Clone / navigate to the project
cd ErrorTriaging

# 2. Build and start all services
docker compose up --build

# 3. Open the UI
open http://localhost:3000

# 4. Watch the logs live
tail -f backend/logs/app.log | python3 -m json.tool

# 5. Poll metrics
watch -n 2 'curl -s http://localhost:8000/metrics | python3 -m json.tool'
```

The **incident generator** starts automatically and cycles through failure modes
every ~1–2 minutes. You can also trigger failures manually via the UI or API.

---

## Failure Modes

### 1. `memory_leak` — Memory and latency degrade over time

**Symptoms:**
- `memory_usage_mb` climbs in `/metrics`
- `latency_ms` increases with each request
- No errors at first; service becomes sluggish

**How to trigger:**
```bash
curl -X POST http://localhost:8000/admin/set-failure \
     -H "Content-Type: application/json" \
     -d '{"mode": "memory_leak"}'
```

**Root cause:** Each checkout appends ~50 KB to an in-memory list that is
never freed. Located in `backend/main.py` inside the `checkout()` function.

**Resolution:** Restart the backend container (`docker compose restart backend`).
This clears the in-process list. The underlying code bug remains.

---

### 2. `code_bug` — NullPointerException on 30% of checkouts

**Symptoms:**
- `error_rate` rises to ~0.30 in `/metrics`
- ERROR logs with `NullPointerException` and a Python stack trace
- Restarting does NOT fix it (the bug is in the code)

**How to trigger:**
```bash
curl -X POST http://localhost:8000/admin/set-failure \
     -H "Content-Type: application/json" \
     -d '{"mode": "code_bug"}'
```

**Root cause:** `_db_get_user()` in `backend/main.py` returns `None` ~30% of
the time. The caller at the line marked `# ROOT CAUSE OF code_bug FAILURE MODE`
dereferences the result without a null check.

**Fix:** Add a guard before the attribute access:
```python
if user is None:
    raise HTTPException(status_code=404, detail="User not found")
```

---

### 3. `payment_outage` — External dependency returns HTTP 503

**Symptoms:**
- All checkout attempts fail with HTTP 502
- Logs show: `Payment provider returned HTTP 503`, retry warnings, then escalation message
- `error_rate` reaches 1.0
- Restarting the backend does NOT help (the outage is external)

**How to trigger:**
```bash
curl -X POST http://localhost:8000/admin/set-failure \
     -H "Content-Type: application/json" \
     -d '{"mode": "payment_outage"}'
```

**Root cause:** The payment provider dependency (`_call_payment_provider()`) is
unavailable. This is an external service issue, not a code bug.

**Resolution:** Escalate to the payments team. Restarting the service will not
help. The correct action is to open an incident with the upstream provider.

---

## API Reference

### `GET /health`
```json
{ "status": "ok", "failure_mode": "none" }
```

### `GET /metrics`
```json
{
  "failure_mode": "none",
  "error_rate": 0.12,
  "latency_ms": 45,
  "memory_usage_mb": 64.0,
  "db_connections": 7,
  "request_count": 42,
  "error_count": 5,
  "uptime_seconds": 184
}
```

### `POST /checkout`
```json
// Request
{ "user_id": "user_001", "cart_id": "cart_abc", "amount": 59.99 }

// Success response
{ "status": "success", "transaction_id": "txn_47821", "user_id": "user_001", "amount": 59.99 }

// Error response
{ "detail": "Internal server error: user object was null. See logs for stack trace." }
```

### `POST /admin/set-failure`
```json
// Request  — mode: none | memory_leak | code_bug | payment_outage
{ "mode": "memory_leak" }
```

### `POST /admin/reset`
Clears all state (failure mode, memory, counters). Simulates a service restart.

---

## Key Files for AI Agent Investigation

| What to look at          | Where                                         |
|--------------------------|-----------------------------------------------|
| Application logs         | `backend/logs/app.log` (JSON, one entry/line) |
| Sample logs (pre-run)    | `backend/logs/sample.log`                     |
| Backend source code      | `backend/main.py`                             |
| Metrics endpoint         | `GET http://localhost:8000/metrics`           |
| Failure injection        | `POST http://localhost:8000/admin/set-failure`|
| Incident generator logic | `incident-generator/generator.py`             |

### Useful commands for an AI agent

```bash
# Read last 50 log lines
tail -50 backend/logs/app.log

# Count ERROR lines
grep '"level": "ERROR"' backend/logs/app.log | wc -l

# Search for NullPointerException in logs
grep "NullPointerException" backend/logs/app.log

# Search for the root cause in source code
grep -n "ROOT CAUSE" backend/main.py

# Get current metrics
curl -s http://localhost:8000/metrics

# Get current health + failure mode
curl -s http://localhost:8000/health

# Reset to normal
curl -X POST http://localhost:8000/admin/reset
```

---

## Project Structure

```
ErrorTriaging/
├── docker-compose.yml
├── README.md
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                  ← FastAPI service (all logic here)
│   └── logs/
│       ├── app.log              ← Written at runtime (volume mount)
│       └── sample.log           ← Pre-populated example logs
│
├── frontend/
│   ├── Dockerfile
│   └── index.html               ← Single-file UI
│
└── incident-generator/
    ├── Dockerfile
    ├── requirements.txt
    └── generator.py             ← Periodic failure injection loop
```

---

## Stopping and Cleaning Up

```bash
# Stop all services
docker compose down

# Stop and remove volumes/images
docker compose down --rmi local

# Clear the log file
> backend/logs/app.log
```
