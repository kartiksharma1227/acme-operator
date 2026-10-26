"""VERIFY: an independent check of the work against the real systems.

The verifier is a fresh conversation with its own browser and read-only tools. It sees the
contract and the operator's *claims*, never the operator's conversation, so it can't simply
inherit the operator's mistakes. Its overall verdict is computed from its findings in code.
"""
from __future__ import annotations

import json
from pathlib import Path
from string import Template

from agent.context import trim
from agent.contract import today_text
from agent.human import ScriptedChannel
from agent.llm import LLM
from agent.loop import run_batch
from agent.models import VerificationReport
from agent.state import RunStore
from agent.tools import RunContext, build_registry
from agent.tools.handbook import policies_text, read_sop, systems_overview

PROMPTS = Path(__file__).resolve().parent / "prompts"
MAX_VERIFY_TURNS = 40
# Verification is short but must see whole collections at once (e.g. every page of a list),
# so it keeps far more full page snapshots in context than the operator does.
VERIFIER_KEEP_FULL = 8


def approval_record(run_dir: Path) -> list[dict]:
    """Approval decisions as logged by the policy gate. The gate is plain code, so this is a
    system of record the operator cannot write to, unlike its own claims."""
    return [{"when": e["ts"], "policy": e["data"].get("rule_id"), "action": e["data"].get("summary"),
             "values": e["data"].get("params"), "approved": e["data"].get("approved"), "note": e["data"].get("note")}
            for e in RunStore(run_dir).events() if e["type"] == "approval_decided"]


def verifier_prompt(ctx: RunContext) -> str:
    state, contract = ctx.state, ctx.state.contract
    sop = read_sop(ctx.config, contract.sop_used) if contract.sop_used else None
    claims = [o.model_dump(exclude={"ts"}) for o in state.outcomes] or ["(no outcomes were recorded)"]
    return Template((PROMPTS / "verifier.md").read_text()).substitute(
        today=today_text(),
        contract=contract.model_dump_json(indent=2, include={"objective", "interpretation", "acceptance_criteria"}),
        claims=json.dumps(claims, indent=2),
        approvals=json.dumps(approval_record(ctx.run_dir), indent=2, default=str) or "[]",
        summary=state.finish_summary or "(the operator did not call finish)",
        sop=sop[1] if sop else "No procedure; the contract is the only reference.",
        policies=policies_text(ctx.config),
        systems=systems_overview(ctx.config),
    )


def verify(parent: RunContext, llm: LLM) -> VerificationReport:
    # Own context and browser; same state (for token and recovery accounting) and the same secrets.
    ctx = RunContext(config=parent.config, state=parent.state, run_dir=parent.run_dir,
                     human=ScriptedChannel(), emit=parent.emit, gate=None, secrets=parent.secrets)
    registry = build_registry().read_only()
    messages: list[dict] = [{"role": "system", "content": verifier_prompt(ctx)},
                            {"role": "user", "content": "Verify the work now."}]
    ctx.emit("verification_started", {"round": len(parent.state.verifications) + 1, "tools": registry.names()})
    try:
        for _ in range(MAX_VERIFY_TURNS):
            response = llm.chat(trim(messages, keep_full=VERIFIER_KEEP_FULL), registry.schemas())
            messages.append(response.assistant_message)
            if not response.tool_calls:
                break
            run_batch(ctx, registry, response.tool_calls, messages)  # same observe-before-acting rules
        messages.append({"role": "user", "content": "Give your verdict for every acceptance criterion and every claim."})
        report = llm.structured(trim(messages, keep_full=VERIFIER_KEEP_FULL), VerificationReport).recompute_overall()
    finally:
        ctx.close()
    parent.state.verifications.append(report.model_dump())
    ctx.emit("verification_result", report.model_dump())
    return report


def repair_message(report: VerificationReport) -> str:
    failed = [f"- {c.criterion_id} ({c.status}): {c.explanation}" for c in report.criteria if c.status != "pass"]
    refuted = [f"- claim '{c.item}' ({c.status}): {c.explanation}" for c in report.claims if c.status != "confirmed"]
    return ("An independent verifier checked your work against the real systems and found problems:\n"
            + "\n".join(failed + refuted)
            + "\n\nFix what can be fixed. Never redo work that already succeeded (duplicates are blocked anyway). "
              "If something can't be fixed or undone, record its outcome honestly. Then call finish again.")
