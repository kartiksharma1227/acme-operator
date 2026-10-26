"""Durable run state: an append-only event log plus a checkpoint after every step.

events.jsonl is the audit trail (what happened, in order). state.json is the
checkpoint (where we are), written atomically so a crash never leaves it half-written.
"""
from __future__ import annotations

import json
import os
import secrets as _secrets
from datetime import datetime
from pathlib import Path

from agent.models import RunState, now
from agent.tools.base import redact


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{_secrets.token_hex(2)}"


class RunStore:
    def __init__(self, run_dir: Path, secrets: set[str] | None = None):
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = run_dir / "events.jsonl"
        self.state_path = run_dir / "state.json"
        self.secrets = secrets if secrets is not None else set()

    def append(self, event_type: str, data: dict, step: int = 0) -> None:
        line = {"ts": now(), "step": step, "type": event_type, "data": redact(data, self.secrets)}
        with self.events_path.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

    def save(self, state: RunState) -> None:
        text = redact(state.model_dump_json(indent=1), self.secrets)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(text)
        os.replace(tmp, self.state_path)  # atomic on POSIX

    def load(self) -> RunState:
        return RunState.model_validate_json(self.state_path.read_text())

    def events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines() if line.strip()]
