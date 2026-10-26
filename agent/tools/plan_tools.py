"""Tools for the operator's own bookkeeping: the living plan, per-item outcomes, and 'I'm done'.

These write only to run state. They are what the report and the verifier read:
the outcomes are the operator's *claims*, which the verifier then checks independently.
"""
from __future__ import annotations

from pydantic import ValidationError

from agent.models import Outcome, PlanStep, ToolResult
from agent.tools.base import Tool

STEP_STATUSES = ["pending", "in_progress", "done", "failed", "skipped"]


class UpdatePlan(Tool):
    name = "update_plan"
    description = ("Replace your working plan (a checklist). Call it at the start, and again whenever "
                   "reality differs from the plan: a step done, failed, skipped, or new steps discovered.")
    parameters = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "status": {"type": "string", "enum": STEP_STATUSES},
                        "note": {"type": "string"},
                    },
                    "required": ["id", "title", "status"],
                },
            },
        },
        "required": ["steps"],
    }
    read_only = False

    def run(self, ctx, steps: list[dict]) -> ToolResult:
        try:
            plan = [PlanStep(**s) for s in steps]
        except ValidationError as exc:
            return ToolResult.fail("validation", f"Invalid plan steps: {exc.errors()[0]['msg']}")
        ctx.state.plan = plan
        ctx.emit("plan_updated", {"steps": [s.model_dump() for s in plan]})
        counts = {status: sum(s.status == status for s in plan) for status in STEP_STATUSES}
        return ToolResult.success({k: v for k, v in counts.items() if v})


class RecordOutcome(Tool):
    name = "record_outcome"
    description = ("Record the final outcome of one unit of work (one document, one record, one sub-task), "
                   "with evidence such as record ids, URLs or screenshot paths. Recording the same item "
                   "again replaces its earlier outcome.")
    parameters = {
        "type": "object",
        "properties": {
            "item": {"type": "string", "description": "Which unit of work, e.g. a document name or record id"},
            "status": {"type": "string", "description": "Short outcome label, e.g. created / skipped / blocked"},
            "details": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["item", "status", "details"],
    }
    read_only = False

    def run(self, ctx, item: str, status: str, details: str, evidence: list[str] | None = None) -> ToolResult:
        outcome = Outcome(item=item, status=status, details=details, evidence=evidence or [])
        ctx.state.outcomes = [o for o in ctx.state.outcomes if o.item != item] + [outcome]
        ctx.emit("outcome_recorded", outcome.model_dump())
        return ToolResult.success({"recorded": item, "total_outcomes": len(ctx.state.outcomes)})


class Finish(Tool):
    name = "finish"
    description = ("Declare the task complete. Only call this when you believe every acceptance criterion is "
                   "met and every unit of work has a recorded outcome. Your work will then be independently verified.")
    parameters = {
        "type": "object",
        "properties": {"summary": {"type": "string", "description": "What was done, in a few sentences"}},
        "required": ["summary"],
    }
    read_only = False

    def run(self, ctx, summary: str) -> ToolResult:
        ctx.state.finished = True
        ctx.state.finish_summary = summary
        return ToolResult.success({"finished": True}, message="Your work will now be independently verified.")


PLAN_TOOLS = [UpdatePlan, RecordOutcome, Finish]
