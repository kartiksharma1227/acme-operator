"""UNDERSTAND: turn a short request into a task contract before anything is touched.

The model reads the handbook (only read tools are available here), then writes a
structured TaskContract. Missing information is asked for up front, in one batch,
and a task with no matching procedure needs its plan approved by a human.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from string import Template

from agent.llm import LLM
from agent.models import TaskContract
from agent.tools import RunContext, ToolRegistry
from agent.tools.handbook import handbook_index, policies_text, read_sop, systems_overview

PROMPTS = Path(__file__).resolve().parent / "prompts"
APPROVAL = re.compile(r"\bapprov", re.IGNORECASE)
HANDBOOK_TOOLS = ("list_handbook", "read_handbook", "get_policies")
MAX_READ_TURNS = 6
MAX_QUESTION_ROUNDS = 2


class PlanRejected(Exception):
    pass


def today_text() -> str:
    today = date.today()
    return f"{today:%A}, {today.isoformat()}"


def _handbook_text(ctx: RunContext) -> str:
    return "\n".join(f"- {e['name']}: {e['title']}. {e['description']} (e.g. {'; '.join(e['triggers'][:3])})"
                     for e in handbook_index(ctx.config)) or "(no procedures)"


def understand(request: str, ctx: RunContext, llm: LLM, registry: ToolRegistry) -> TaskContract:
    """Draft the contract, settle open questions, get the plan approved if needed.

    The draft is kept in run state, so if this stage pauses for a human, resuming continues from
    the *same* draft instead of regenerating it (a regenerated draft can phrase things differently
    and no longer match the answer the human gave)."""
    contract = ctx.state.draft_contract or _draft(request, ctx, llm, registry)
    ctx.state.draft_contract = contract

    for _ in range(MAX_QUESTION_ROUNDS):
        blocking = _blocking_questions(contract)
        if not blocking:
            break
        question = "Before I start, I need to know:\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(blocking, 1))
        ctx.emit("question_asked", {"question": question, "stage": "contract"})
        answer = ctx.human.ask("question", {"question": question}).answer
        ctx.emit("question_answered", {"question": question, "answer": answer, "stage": "contract"})
        ctx.state.bump("human_questions")
        contract = _revise(request, contract, answer, ctx, llm)
        ctx.state.draft_contract = contract

    ctx.emit("contract_created", contract.model_dump())

    if contract.sop_used is None and ctx.config.policies.get("unknown_task", {}).get("require_plan_approval"):
        payload = {"summary": "No company procedure covers this request. Approve this plan before I act?",
                   "rule_id": "unknown_task", "rule": "Tasks without a procedure need their plan approved.",
                   "params": {"objective": contract.objective,
                              "plan": " → ".join(contract.initial_plan)}}
        ctx.emit("approval_requested", payload)
        response = ctx.human.ask("approval", payload)
        ctx.emit("approval_decided", {**payload, "approved": bool(response.approved), "note": response.note})
        if not response.approved:
            raise PlanRejected(response.note or "The plan was not approved.")
        # The executor only sees the contract, so it must know this approval already happened.
        note = f" ({response.note})" if response.note else ""
        contract = contract.model_copy(update={"assumptions": [
            *contract.assumptions,
            f"A human approved this plan before work started{note}; no further approval is needed to carry it out.",
        ]})
        ctx.emit("contract_created", contract.model_dump())
    return contract


def _system_prompt(ctx: RunContext) -> str:
    return Template((PROMPTS / "contract.md").read_text()).substitute(
        today=today_text(), handbook=_handbook_text(ctx),
        policies=policies_text(ctx.config), systems=systems_overview(ctx.config))


def _draft(request: str, ctx: RunContext, llm: LLM, registry: ToolRegistry) -> TaskContract:
    readers = ToolRegistry([registry.tools[name] for name in HANDBOOK_TOOLS])
    messages: list[dict] = [{"role": "system", "content": _system_prompt(ctx)},
                            {"role": "user", "content": f"Request: {request}"}]
    # Let the model open the procedures it needs before committing to a contract.
    for _ in range(MAX_READ_TURNS):
        response = llm.chat(messages, readers.schemas())
        messages.append(response.assistant_message)
        if not response.tool_calls:
            break
        for call in response.tool_calls:
            result = readers.run(ctx, call.name, call.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result.to_llm()})
    messages.append({"role": "user", "content": "Write the task contract for this request now."})
    return _write_contract(request, messages, llm)


def _revise(request: str, draft: TaskContract, answer: str, ctx: RunContext, llm: LLM) -> TaskContract:
    """Update the draft with the requester's answers. Self-contained, so it also works after a resume."""
    sop = read_sop(ctx.config, draft.sop_used) if draft.sop_used else None
    messages = [{"role": "system", "content": _system_prompt(ctx)},
                {"role": "user", "content": f"Request: {request}"
                                            + (f"\n\nThe procedure:\n{sop[1]}" if sop else "")
                                            + f"\n\nYour draft task contract:\n{draft.model_dump_json(indent=2)}"
                                            + f"\n\nAnswers from the requester:\n{answer}\n\n"
                                              "Update the task contract with these answers."}]
    return _write_contract(request, messages, llm)


def _blocking_questions(contract: TaskContract) -> list[str]:
    # Approvals are never questions: plan approval and policy approvals have their own, exact routes.
    return [q.question for q in contract.open_questions if q.blocking and not APPROVAL.search(q.question)]


def _write_contract(request: str, messages: list[dict], llm: LLM) -> TaskContract:
    contract = llm.structured(messages, TaskContract)
    return contract.model_copy(update={"request": request})
