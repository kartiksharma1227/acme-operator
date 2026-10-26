"""How the operator reaches a human: for approvals and for questions it can't answer itself.

One interface, several channels. The runtime never knows which one is plugged in.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from agent.models import RunState, now

Kind = Literal["approval", "question"]


class HumanResponse(BaseModel):
    approved: bool | None = None  # set for approvals
    answer: str = ""              # set for questions
    note: str = ""


class HumanChannel(Protocol):
    def ask(self, kind: Kind, payload: dict) -> HumanResponse: ...


class TerminalChannel:
    """Blocks on the terminal. Default for interactive runs."""

    def __init__(self, console: Console | None = None):
        self.console = console or Console()

    def ask(self, kind: Kind, payload: dict) -> HumanResponse:
        try:
            return self._ask(kind, payload)
        except EOFError:
            # Nobody can answer (input closed): never treat silence as approval.
            self.console.print("[red]No human input available; treating this as not approved.[/red]")
            return HumanResponse(approved=False, note="no human response (input closed)")

    def _ask(self, kind: Kind, payload: dict) -> HumanResponse:
        if kind == "approval":
            lines = [f"[bold]{payload.get('summary', 'Approval needed')}[/bold]"]
            if payload.get("rule_id"):
                lines.append(f"Policy {payload['rule_id']}: {payload.get('rule', '')}")
            for key, value in (payload.get("params") or {}).items():
                lines.append(f"  {key}: {value}")
            if payload.get("reason"):
                lines.append(f"Operator's reason: {payload['reason']}")
            self.console.print(Panel("\n".join(lines), title="⏸  Approval required", border_style="magenta"))
            approved = Confirm.ask("Approve?", console=self.console)
            note = Prompt.ask("Note (optional)", default="", console=self.console)
            return HumanResponse(approved=approved, note=note)

        options = payload.get("options") or []
        body = payload["question"] + (f"\nOptions: {', '.join(options)}" if options else "")
        self.console.print(Panel(body, title="❓ The operator has a question", border_style="cyan"))
        return HumanResponse(answer=Prompt.ask("Answer", console=self.console))


class RunPaused(Exception):
    """Control flow, not an error: the run stops here and waits for a human, possibly for hours."""

    def __init__(self, request: dict):
        super().__init__(f"waiting for a human ({request['kind']})")
        self.request = request


def request_key(kind: Kind, payload: dict) -> str:
    from agent.policy import canonical_params  # local import: policy depends on models, not on this module

    basis = {"kind": kind, "rule": payload.get("rule_id"), "params": canonical_params(payload.get("params") or {})}
    if kind == "question":
        basis["question"] = " ".join(str(payload.get("question", "")).lower().split())
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:16]


class FileChannel:
    """Durable human-in-the-loop: write the request to disk, pause the run, and exit.

    `python -m agent resume <run> --approve|--deny|--answer` stores the human's response in the
    run state and continues; when the same request comes up again it is answered from there.
    """

    def __init__(self, state: RunState, run_dir: Path):
        self.state = state
        self.run_dir = run_dir

    def ask(self, kind: Kind, payload: dict) -> HumanResponse:
        if (stored := self._stored_response(kind, payload)) is not None:
            return stored
        pending = {"key": request_key(kind, payload), "kind": kind, "payload": payload, "asked_at": now()}
        (self.run_dir / "pending.json").write_text(json.dumps(pending, indent=2, default=str))
        raise RunPaused(pending)

    def _stored_response(self, kind: Kind, payload: dict) -> HumanResponse | None:
        responses = self.state.human_responses
        exact = responses.get(request_key(kind, payload))
        # An approval covers exactly the action the human saw (its values are read from the page, so
        # they don't drift); any other action under the same policy must be asked about again.
        # Questions are different: after a resume the model may phrase the same question differently,
        # so one unused answer may serve a rephrased question.
        entry = exact or (None if kind == "approval" else next(
            (r for r in responses.values() if not r["consumed"] and r["kind"] == kind), None))
        if entry is None:
            return None
        entry["consumed"] = True
        return HumanResponse(**entry["response"])


class ScriptedChannel:
    """Answers from a script, for tests and evals. Records every request it receives."""

    def __init__(self, approvals: dict[str, str] | None = None, answers: list[str] | None = None,
                 default_approval: str = "deny"):
        self.approvals = approvals or {}
        self.answers = list(answers or [])
        self.default_approval = default_approval
        self.asked: list[tuple[Kind, dict]] = []

    def ask(self, kind: Kind, payload: dict) -> HumanResponse:
        self.asked.append((kind, payload))
        if kind == "approval":
            decision = self.approvals.get(payload.get("rule_id") or "", self.approvals.get("*", self.default_approval))
            return HumanResponse(approved=decision == "approve", note=f"scripted: {decision}")
        answer = self.answers.pop(0) if self.answers else "No further information is available."
        return HumanResponse(answer=answer)
