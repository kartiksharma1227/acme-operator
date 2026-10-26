"""Runtime-level recovery: notice when the operator is stuck, and say so.

Tools already recover from what they can handle alone (retrying server errors, reporting a
lost session, listing alternatives when a control isn't found). This module watches the
run as a whole, in plain code:

  - the same call failing again and again        -> reflect: "stop, try something else"
  - many turns with no progress at all           -> reflect
  - still no progress after two reflections      -> escalate to a human
  - a failure followed by its fix                -> recorded as a recovery for the report
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent.models import ToolResult
from agent.policy import canonical

if TYPE_CHECKING:
    from agent.tools.base import RunContext

REPEAT_LIMIT = 3              # identical failing calls in a row before reflecting
STALL_TURNS = 20              # turns without progress before reflecting (slow systems need room)
REFLECTIONS_BEFORE_HUMAN = 2  # reflections without progress before asking a human

REFLECTION = ("You seem to be stuck: {reason}. Stop repeating that. Re-read the latest page, update your plan, "
              "and try a different approach. If you are genuinely blocked, ask a human.")


def note_recovery(ctx: "RunContext", kind: str, **details) -> None:
    """The single place recoveries are recorded, so the report can count them by type."""
    ctx.state.bump(f"recovery:{kind}")
    ctx.emit("recovery", {"type": kind, **details})


def _brief(args: dict) -> str:
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())


class RecoveryMonitor:
    def __init__(self, ctx: "RunContext"):
        self.ctx = ctx
        self._fail_key: str | None = None
        self._fail_count = 0
        self._stuck_reason: str | None = None
        self._last_progress = self._progress()
        self._idle_turns = 0
        self._reflections = 0  # since the last progress
        self._recent_errors: list[str] = []
        # failures a later success would "recover" from
        self._missing_control: dict | None = None
        self._rejected_submit: dict | None = None
        self._lost_session = False

    def _progress(self) -> tuple:
        state = self.ctx.state
        return len(state.outcomes), tuple((s.id, s.status) for s in state.plan), len(state.action_ledger)

    # ---------- per tool call ----------

    def observe(self, tool: str, args: dict, result: ToolResult) -> None:
        if result.ok:
            self._fail_key, self._fail_count = None, 0
            self._recognise_recovery(tool, args)
            return

        key = json.dumps([tool, {k: canonical(v) for k, v in args.items()}], sort_keys=True, default=str)
        self._fail_count = self._fail_count + 1 if key == self._fail_key else 1
        self._fail_key = key
        self._recent_errors = [*self._recent_errors, f"{tool}: {result.error_type}: {result.message[:120]}"][-5:]
        if self._fail_count >= REPEAT_LIMIT:
            self._stuck_reason = f"{tool}({_brief(args)}) failed {self._fail_count} times in a row ({result.error_type})"

        if result.error_type == "auth":
            self._lost_session = True
        elif tool == "browser_click" and result.error_type == "not_found":
            self._missing_control = args
        elif tool == "browser_click" and result.error_type == "validation":
            self._rejected_submit = args

    def _recognise_recovery(self, tool: str, args: dict) -> None:
        if tool != "browser_click":
            return
        browser = getattr(self.ctx, "_browser", None)
        if self._lost_session and browser is not None and browser.logged_in:
            note_recovery(self.ctx, "reauthenticated")
            self._lost_session = False
        missing = self._missing_control
        if missing and args.get("role") == missing.get("role") and args.get("name") != missing.get("name"):
            note_recovery(self.ctx, "element_relocated", **{"from": missing.get("name"), "to": args.get("name")})
            self._missing_control = None
        if self._rejected_submit and args == self._rejected_submit:
            note_recovery(self.ctx, "input_corrected", control=args.get("name"))
            self._rejected_submit = None

    # ---------- per model turn ----------

    def end_turn(self) -> str | None:
        """Returns a message for the model when it needs steering, else None."""
        progress = self._progress()
        if progress != self._last_progress:
            self._last_progress, self._idle_turns, self._reflections = progress, 0, 0
        else:
            self._idle_turns += 1

        reason = self._stuck_reason
        if reason is None and self._idle_turns >= STALL_TURNS:
            reason = (f"no progress in {self._idle_turns} turns (no outcome recorded, plan change "
                      "or completed action)")
        if reason is None:
            return None
        self._stuck_reason, self._fail_key, self._fail_count, self._idle_turns = None, None, 0, 0

        if self._reflections >= REFLECTIONS_BEFORE_HUMAN:
            return self._escalate(reason)
        self._reflections += 1
        note_recovery(self.ctx, "reflection", reason=reason)
        return REFLECTION.format(reason=reason)

    def _escalate(self, reason: str) -> str:
        self._reflections = 0
        errors = "\n".join(f"- {e}" for e in self._recent_errors) or "- (none)"
        question = (f"The operator is stuck and needs guidance: {reason}.\nRecent errors:\n{errors}\n"
                    "How should it proceed?")
        note_recovery(self.ctx, "escalated", reason=reason)
        self.ctx.emit("question_asked", {"question": question, "stage": "escalation"})
        answer = self.ctx.human.ask("question", {"question": question}).answer
        self.ctx.emit("question_answered", {"question": question, "answer": answer, "stage": "escalation"})
        self.ctx.state.bump("human_questions")
        return (f"You were stuck ({reason}), so a human was asked for guidance. Their answer: {answer}\n"
                "Follow it, and record honestly any unit of work you cannot complete.")
