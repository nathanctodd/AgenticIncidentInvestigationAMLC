"""
incident-generator: Periodically injects failure modes into the checkout service.

Behavior is controlled by the AUTO_INCIDENTS environment variable:

  AUTO_INCIDENTS=false (default)
    The generator starts, waits for the backend, then idles.
    All failures must be triggered manually via the frontend UI or API.
    Use this mode during demos before the agent is set up.

  AUTO_INCIDENTS=true
    The generator cycles automatically:
      1. Clear any active failure (normal traffic for ~30 s)
      2. Randomly pick a failure mode and inject it
      3. Send checkout traffic during the failure window (~40 s)
      4. Repeat
    Use this mode once the agent is ready to investigate on its own.

Set the flag in docker-compose.yml under incident-generator > environment.
"""

import logging
import os
import random
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] generator — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("generator")

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
AUTO_INCIDENTS = os.getenv("AUTO_INCIDENTS", "false").lower() == "true"

FAILURE_MODES = ["memory_leak", "code_bug", "payment_outage"]

# Realistic-looking test carts
SAMPLE_CARTS = [
    {"user_id": "user_001", "cart_id": "cart_abc", "amount": 59.99},
    {"user_id": "user_002", "cart_id": "cart_def", "amount": 124.50},
    {"user_id": "user_003", "cart_id": "cart_ghi", "amount": 19.99},
    {"user_id": "user_004", "cart_id": "cart_jkl", "amount": 250.00},
    {"user_id": "user_005", "cart_id": "cart_mno", "amount": 9.99},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(path: str, body: dict) -> requests.Response | None:
    try:
        return requests.post(f"{BACKEND_URL}{path}", json=body, timeout=15)
    except requests.RequestException as exc:
        logger.error(f"POST {path} failed: {exc}")
        return None


def set_failure(mode: str) -> None:
    resp = _post("/admin/set-failure", {"mode": mode})
    if resp and resp.ok:
        logger.info(f"Failure mode set to: {mode!r}")
    elif resp:
        logger.error(f"Failed to set mode {mode!r}: {resp.status_code} {resp.text}")


def send_checkout(cart: dict) -> None:
    try:
        resp = requests.post(f"{BACKEND_URL}/checkout", json=cart, timeout=15)
        if resp.ok:
            data = resp.json()
            logger.info(
                f"Checkout OK — user={cart['user_id']} "
                f"transaction={data.get('transaction_id')} amount=${cart['amount']}"
            )
        else:
            data = resp.json()
            logger.warning(
                f"Checkout failed ({resp.status_code}) — user={cart['user_id']} "
                f"error={data.get('detail', 'unknown')}"
            )
    except requests.RequestException as exc:
        logger.error(f"Checkout request error for user={cart['user_id']}: {exc}")


def wait_for_backend(max_wait_s: int = 60) -> None:
    logger.info(f"Waiting for backend at {BACKEND_URL} ...")
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        try:
            resp = requests.get(f"{BACKEND_URL}/health", timeout=3)
            if resp.ok:
                logger.info("Backend is ready.")
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    logger.error("Backend did not become ready within timeout. Proceeding anyway.")


def send_traffic(count: int, min_gap: float = 2.0, max_gap: float = 5.0) -> None:
    """Send `count` checkout requests with random delays."""
    for _ in range(count):
        cart = random.choice(SAMPLE_CARTS)
        send_checkout(cart)
        time.sleep(random.uniform(min_gap, max_gap))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    wait_for_backend()

    if not AUTO_INCIDENTS:
        logger.info(
            "AUTO_INCIDENTS=false — generator is idle. "
            "Trigger failures manually via the UI or POST /admin/set-failure."
        )
        while True:
            time.sleep(60)   # stay alive but do nothing

    logger.info("AUTO_INCIDENTS=true — beginning automatic failure cycling.")

    while True:
        # ---- Phase 1: Normal operation ----
        logger.info("=== Phase: NORMAL — no failures active ===")
        set_failure("none")
        send_traffic(count=6, min_gap=3, max_gap=6)   # ~18–36 s of normal traffic

        # ---- Phase 2: Inject a random failure ----
        mode = random.choice(FAILURE_MODES)
        logger.warning(f"=== Phase: FAILURE INJECTION — mode={mode!r} ===")
        set_failure(mode)
        send_traffic(count=8, min_gap=2, max_gap=5)   # ~16–40 s under failure

        # ---- Phase 3: Short cooldown before next cycle ----
        cooldown = random.randint(10, 20)
        logger.info(f"=== Phase: COOLDOWN — sleeping {cooldown}s before next cycle ===")
        time.sleep(cooldown)


if __name__ == "__main__":
    run()
