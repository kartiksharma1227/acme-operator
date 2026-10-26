"""EXECUTE / OBSERVE / ADAPT: the operator's main loop.

Each turn: the model looks at the conversation so far (contract, procedure, every tool
result) and picks the next actions; we run them and feed back what happened. The model
adapts to whatever it observes. The loop ends when the model calls `finish`, runs out
of budget, or stops making progress.
"""
from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import Callable

from agent.context import trim
from agent.contract import today_text
from agent.llm import LLM, ToolCall
from agent.models import TaskContract, ToolResult
from agent.recovery import RecoveryMonitor
from agent.tools import RunContext, ToolRegistry
from agent.tools.base import redact
from agent.tools.handbook import policies_text, read_sop, systems_overview

PROMPTS = Path(__file__).resolve().parent / "prompts"
MAX_NUDGES = 2
NUDGE = ("Continue the task using the tools. If every unit of work has a recorded outcome and the "
         "acceptance criteria are met, call `finish`; if you are blocked, use `ask_human`.")


def executor_prompt(ctx: RunContext, contract: TaskContract) -> str:
    sop = read_sop(ctx.config, contract.sop_used) if contract.sop_used else None
    return Template((PROMPTS / "executor.md").read_text()).substitute(
        today=today_text(),
        contract=contract.model_dump_json(indent=2),
        sop=sop[1] if sop else "No procedure covers this task. Follow the approved plan in the contract.",
        policies=policies_text(ctx.config),
        systems=systems_overview(ctx.config),
    )


class Executor:
    def __init__(self, ctx: RunContext, llm: LLM, registry: ToolRegistry,
                 checkpoint: Callable[[], None] = lambda: None):
        self.ctx, self.llm, self.registry, self.checkpoint = ctx, llm, registry, checkpoint
        self.monitor = RecoveryMonitor(ctx)

    def start(self, contract: TaskContract) -> None:
        self.ctx.state.messages = [
            {"role": "system", "content": executor_prompt(self.ctx, contract)},
            {"role": "user", "content": f"Request: {contract.request}"},
        ]

    def run(self) -> str:
        """Returns 'finished', 'budget' or 'stuck'."""
        state, config = self.ctx.state, self.ctx.config
        schemas = self.registry.schemas()
        nudges = 0
        while not state.finished:
            if state.step >= config.max_steps:
                self.ctx.emit("budget_exceeded", {"kind": "steps", "limit": config.max_steps})
                return "budget"
            if self.llm.has_prices and state.cost_usd >= config.max_cost_usd:
                self.ctx.emit("budget_exceeded", {"kind": "cost", "limit": config.max_cost_usd})
                return "budget"

            state.step += 1
            response = self.llm.chat(trim(state.messages), schemas)
            state.messages.append(response.assistant_message)
            if response.content:
                self.ctx.emit("assistant_note", {"text": response.content[:1000]})

            if not response.tool_calls:
                nudges += 1
                if nudges > MAX_NUDGES:
                    self.ctx.emit("stuck", {"reason": "no tool calls", "nudges": nudges - 1})
                    self.checkpoint()
                    return "stuck"
                state.messages.append({"role": "user", "content": NUDGE})
            else:
                nudges = 0
                self._run_batch(response.tool_calls)
            if steer := self.monitor.end_turn():
                state.messages.append({"role": "user", "content": steer})
            self.checkpoint()
        return "finished"

    def _run_batch(self, calls: list[ToolCall]) -> None:
        run_batch(self.ctx, self.registry, calls, self.ctx.state.messages,
                  observe=self.monitor.observe, stopped=lambda: self.ctx.state.finished)


def run_batch(ctx: RunContext, registry: ToolRegistry, calls: list[ToolCall], messages: list[dict],
              observe: Callable[[str, dict, ToolResult], None] | None = None,
              stopped: Callable[[], bool] = lambda: False) -> None:
    """Run one turn's tool calls in order, appending a result message for each (the API requires one
    per call). Shared by the operator and the verifier, so both observe before acting:

    the rest of the batch is skipped after a failure, or after an action that changed the view,
    because those calls were chosen before the model could see what happened."""
    failed: str | None = None
    view_changed: str | None = None
    for call in calls:
        if stopped():
            result = ToolResult.fail("validation", "Skipped: finish was already called in this turn.")
        elif failed:
            result = ToolResult.fail("validation", f"Skipped: an earlier action in this batch ({failed}) "
                                                   "failed. Re-observe, then continue.")
        elif view_changed:
            result = ToolResult.fail("validation", f"Skipped: {view_changed} changed the page earlier in this "
                                                   "batch. Look at its result before acting again.")
        else:
            result = registry.run(ctx, call.name, call.arguments)
            if observe:
                observe(call.name, _args(call), result)
            tool = registry.tools.get(call.name)
            if not result.ok:
                failed = call.name
            elif tool is not None and tool.changes_view:
                view_changed = call.name
        if result.message.startswith("Skipped:"):
            ctx.emit("tool_skipped", {"tool": call.name, "args": _args(call), "reason": result.message})
        messages.append({"role": "tool", "tool_call_id": call.id, "content": redact(result.to_llm(), ctx.secrets)})


def _args(call: ToolCall) -> dict:
    try:
        return json.loads(call.arguments or "{}")
    except json.JSONDecodeError:
        return {"raw": call.arguments}
