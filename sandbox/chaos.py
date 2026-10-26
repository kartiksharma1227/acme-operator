"""Failure injection for the sandbox, so the operator's recovery can be shown on demand.

Modes (env CHAOS, comma-separated):
  rename  - submit buttons get different labels (UI drift)
  expiry  - sessions expire after CHAOS_EXPIRY_N authenticated requests (enforced in app.require_user)
  flaky   - a share of GET requests return a 500 page (transient errors)
  slow    - every response is delayed 0.5-3s
CHAOS_SEED makes the random parts reproducible for demos.
"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.responses import HTMLResponse

# key -> (normal label, label under chaos "rename")
BUTTON_LABELS: dict[str, tuple[str, str]] = {
    "create_bill": ("Create Bill", "Save Bill"),
    "log_exception": ("Log Exception", "Submit Exception"),
    "assign_equipment": ("Assign Equipment", "Assign Asset"),
    "submit_request": ("Submit Request", "Send Request"),
    "create_employee": ("Create Employee", "Create Employee"),
}

# Never break the pages needed to recover (login), to observe chaos, or to health-check.
FLAKY_EXEMPT_PREFIXES = ("/login", "/static", "/health", "/admin/chaos")

FLAKY_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>500 Server Error</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body><main class="error-page">
<h1>500 Server Error</h1>
<p role="alert">Temporary server error, please retry.</p>
</main></body></html>"""


@dataclass
class ChaosConfig:
    modes: set[str] = field(default_factory=set)
    seed: int = 42
    expiry_n: int = 15
    flaky_rate: float = 0.15
    rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    @classmethod
    def from_env(cls) -> "ChaosConfig":
        modes = {m.strip() for m in os.environ.get("CHAOS", "").split(",") if m.strip()}
        return cls(
            modes=modes,
            seed=int(os.environ.get("CHAOS_SEED", "42")),
            expiry_n=int(os.environ.get("CHAOS_EXPIRY_N", "15")),
            flaky_rate=float(os.environ.get("CHAOS_FLAKY_RATE", "0.15")),
        )

    def enabled(self, mode: str) -> bool:
        return mode in self.modes


def label(config: ChaosConfig, key: str) -> str:
    normal, renamed = BUTTON_LABELS[key]
    return renamed if config.enabled("rename") else normal


async def chaos_middleware(request: Request, call_next):
    config: ChaosConfig = request.app.state.chaos
    path = request.url.path
    if path == "/health":  # infrastructure (start-up probes), not part of the app under test
        return await call_next(request)

    if config.enabled("slow") and not path.startswith("/static"):
        await asyncio.sleep(config.rng.uniform(0.5, 3.0))

    # GET only: failing a POST would leave the caller unsure whether the write happened.
    if (
        config.enabled("flaky")
        and request.method == "GET"
        and not path.startswith(FLAKY_EXEMPT_PREFIXES)
        and config.rng.random() < config.flaky_rate
    ):
        return HTMLResponse(FLAKY_PAGE, status_code=500)

    return await call_next(request)
