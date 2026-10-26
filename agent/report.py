"""The evidence report: one self-contained HTML page per run, built from the run's own records.

Everything in it comes from state.json and events.jsonl (already redacted at write time),
so the report can always be regenerated and never shows more than the audit trail holds.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.state import RunStore

TEMPLATES = Path(__file__).resolve().parent / "templates"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
HIDDEN_EVENTS = {"llm_call"}  # model traffic is summarised in the stats instead


def _duration(start: str, end: str) -> str:
    try:
        seconds = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except ValueError:
        return "n/a"
    return f"{seconds // 60} min {seconds % 60} s"


def build_context(store: RunStore) -> dict:
    state = store.load()
    events = store.events()
    of_type = lambda t: [e for e in events if e["type"] == t]  # noqa: E731
    recoveries = Counter(e["data"]["type"] for e in of_type("recovery"))
    blocks = [e["data"] for e in of_type("policy_check") if e["data"]["decision"] in ("deny", "duplicate_blocked", "denied_by_human", "check_failed")]
    models = sorted({e["data"]["model"] for e in of_type("llm_call")})
    return {
        "state": state,
        "contract": state.contract,
        "duration": _duration(state.started_at, state.ended_at or (events[-1]["ts"] if events else state.started_at)),
        "outcome_counts": Counter(o.status for o in state.outcomes),
        "verification": state.verifications[-1] if state.verifications else None,
        "repair_rounds": state.counters.get("repair_rounds", 0),
        "approvals": [e for e in of_type("approval_decided")],
        "questions": [e for e in of_type("question_answered")],
        "recoveries": recoveries,
        "policy_blocks": blocks,
        "models": models,
        "timeline": [e for e in events if e["type"] not in HIDDEN_EVENTS],
        "is_image": lambda p: str(p).lower().endswith(IMAGE_SUFFIXES),
        "screenshots": sorted(p.relative_to(store.run_dir).as_posix()
                              for p in (store.run_dir / "screenshots").glob("*.png")),
    }


def write_report(run_dir: Path) -> Path:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    html = env.get_template("report.html.j2").render(**build_context(RunStore(run_dir)))
    path = run_dir / "report.html"
    path.write_text(html)
    return path
