"""Core data shapes shared by the runtime, tools, verifier and report."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ErrorType = Literal[
    "transient", "not_found", "ambiguous", "auth", "validation",
    "policy_denied", "needs_approval", "budget", "fatal",
]
Risk = Literal["read", "write", "irreversible"]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ToolResult(BaseModel):
    ok: bool
    data: Any = None              # what the model sees on success (kept compact)
    error_type: ErrorType | None = None
    message: str = ""             # on errors, says how to recover
    evidence: list[str] = Field(default_factory=list)  # screenshot paths, URLs, record ids

    @classmethod
    def success(cls, data: Any = None, message: str = "", evidence: list[str] | None = None) -> "ToolResult":
        return cls(ok=True, data=data, message=message, evidence=evidence or [])

    @classmethod
    def fail(cls, error_type: ErrorType, message: str, data: Any = None,
             evidence: list[str] | None = None) -> "ToolResult":
        return cls(ok=False, error_type=error_type, message=message, data=data, evidence=evidence or [])

    def to_llm(self) -> str:
        payload = self.model_dump(exclude_none=True)
        if not payload.get("evidence"):
            payload.pop("evidence", None)
        if not payload.get("message"):
            payload.pop("message", None)
        return json.dumps(payload, ensure_ascii=False, default=str)


class PlanStep(BaseModel):
    id: str
    title: str
    status: Literal["pending", "in_progress", "done", "failed", "skipped"] = "pending"
    note: str = ""


class Outcome(BaseModel):
    item: str                     # the unit of work, e.g. one document or one sub-task
    status: str                   # free-form label chosen by the executor
    details: str = ""
    evidence: list[str] = Field(default_factory=list)
    ts: str = Field(default_factory=now)


class AcceptanceCriterion(BaseModel):
    id: str
    description: str
    how_to_verify: str


class OpenQuestion(BaseModel):
    question: str
    blocking: bool


class TaskContract(BaseModel):
    request: str
    objective: str                # the outcome in one sentence
    sop_used: str | None          # handbook entry name, or None
    interpretation: str           # what is being asked, incl. resolved dates/names
    inputs: list[str]             # sources / systems involved
    assumptions: list[str]
    open_questions: list[OpenQuestion]
    acceptance_criteria: list[AcceptanceCriterion]
    expected_approvals: list[str]  # policy rule ids likely to apply
    initial_plan: list[str]


class CriterionResult(BaseModel):
    criterion_id: str
    status: Literal["pass", "fail", "unverifiable"]
    evidence: list[str]           # record ids, URLs, screenshot paths actually looked at
    explanation: str


class ClaimCheck(BaseModel):
    item: str
    # confirmed: the system shows exactly what was claimed. refuted: it shows something else.
    # unverifiable: the verifier couldn't see it (not evidence of a mistake).
    status: Literal["confirmed", "refuted", "unverifiable"]
    explanation: str


class VerificationReport(BaseModel):
    criteria: list[CriterionResult]
    claims: list[ClaimCheck]
    overall: Literal["pass", "fail", "partial"]
    summary: str

    def recompute_overall(self) -> "VerificationReport":
        """The verdict follows from the findings, not from the model's own summary of them.
        fail = something is demonstrably wrong (worth repairing); partial = couldn't confirm everything."""
        if any(c.status == "fail" for c in self.criteria) or any(c.status == "refuted" for c in self.claims):
            overall = "fail"
        elif (self.criteria and all(c.status == "pass" for c in self.criteria)
              and all(c.status == "confirmed" for c in self.claims)):
            overall = "pass"
        else:
            overall = "partial"
        return self.model_copy(update={"overall": overall})


class RunState(BaseModel):
    run_id: str
    request: str = ""
    status: Literal["running", "awaiting_human", "verifying", "completed", "partial", "failed", "incomplete"] = "running"
    contract: TaskContract | None = None
    draft_contract: TaskContract | None = None  # kept while understanding may still pause for a human
    plan: list[PlanStep] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)
    finished: bool = False
    finish_summary: str = ""
    counters: dict[str, int] = Field(default_factory=dict)
    started_at: str = Field(default_factory=now)
    ended_at: str = ""
    # Full conversation with the executor model; enough to resume a paused run.
    messages: list[dict] = Field(default_factory=list)
    grants: list[str] = Field(default_factory=list)   # approval keys a human has granted
    action_ledger: list[dict] = Field(default_factory=list)  # meaningful writes that succeeded (idempotency)
    human_responses: dict[str, dict] = Field(default_factory=dict)  # answers given on resume, by request key
    human_mode: str = "terminal"
    verifications: list[dict] = Field(default_factory=list)  # one VerificationReport per round
    step: int = 0                                      # model turns taken by the executor
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    def bump(self, counter: str, by: int = 1) -> None:
        self.counters[counter] = self.counters.get(counter, 0) + by
