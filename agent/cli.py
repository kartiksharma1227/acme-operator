"""Command line: run a request, resume a paused run, list runs.

    python -m agent run "<request>" [--human terminal|file|auto-approve|auto-deny]
    python -m agent resume <run_id> (--approve | --deny | --answer TEXT) [--note TEXT]
    python -m agent verify <run_id>        # re-check against the systems as they are now
    python -m agent report <run_id> [--open]
    python -m agent runs
"""
from __future__ import annotations

import argparse
import json
import os
import traceback
import webbrowser
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from agent.config import Config, load_config
from agent.contract import PlanRejected, understand
from agent.human import FileChannel, HumanChannel, HumanResponse, RunPaused, ScriptedChannel, TerminalChannel
from agent.llm import LLM
from agent.loop import Executor
from agent.models import RunState, now
from agent.policy import PolicyGate
from agent.report import write_report
from agent.state import RunStore, new_run_id
from agent.tools import RunContext, ToolRegistry, build_registry
from agent.ui import ConsoleUI
from agent.verifier import repair_message, verify

MAX_REPAIRS = 2
EXIT_CODES = {"completed": 0, "awaiting_human": 3}
PAUSED_RESULT = ("Paused for a human decision before this ran. The run has since been resumed with a new "
                 "browser. Re-observe, and redo this action if it is still needed.")


def make_llm(config: Config, state: RunState, emit) -> LLM:
    """Overridden in tests with a scripted model."""
    return LLM(config.agent_model, state, emit, reasoning_effort=os.environ.get("REASONING_EFFORT"))


def make_verifier_llm(config: Config, state: RunState, emit) -> LLM:
    """A separate model instance (optionally a different model) for independent verification."""
    return LLM(config.verifier_model or config.agent_model, state, emit,
               reasoning_effort=os.environ.get("REASONING_EFFORT"))


def make_channel(kind: str, console: Console, state: RunState, store: RunStore) -> HumanChannel:
    if kind == "auto-approve":
        return ScriptedChannel(approvals={"*": "approve"})
    if kind == "auto-deny":
        return ScriptedChannel(approvals={"*": "deny"})
    if kind == "file":
        return FileChannel(state, store.run_dir)
    return TerminalChannel(console)


@dataclass
class Session:
    config: Config
    state: RunState
    store: RunStore
    ui: ConsoleUI
    ctx: RunContext
    llm: LLM
    registry: ToolRegistry


def open_session(config: Config, state: RunState, store: RunStore, console: Console) -> Session:
    """Everything a run needs, wired the same way for a fresh run and for a resume."""
    ui = ConsoleUI(console)
    ctx = RunContext(config=config, state=state, run_dir=store.run_dir,
                     human=make_channel(state.human_mode, console, state, store))

    def emit(event_type: str, data: dict) -> None:
        store.append(event_type, data, state.step)
        ui.on_event(event_type, data)

    ctx.emit = emit
    store.secrets = ctx.secrets  # the store redacts whatever secrets the tools learn about
    ctx.gate = PolicyGate(config)
    return Session(config, state, store, ui, ctx, make_llm(config, state, emit), build_registry())


def verify_and_repair(s: Session, executor: Executor, outcome: str) -> str:
    """VERIFY, and if verification fails, let the operator REPAIR and verify again.
    The run is `completed` only when the independent verifier passes it."""
    verifier_llm = make_verifier_llm(s.config, s.state, s.ctx.emit)
    for round_ in range(MAX_REPAIRS + 1):
        s.state.status = "verifying"
        s.store.save(s.state)
        report = verify(s.ctx, verifier_llm)
        s.store.save(s.state)
        if report.overall == "pass":
            return "completed"
        # Only something demonstrably wrong is worth a repair; "couldn't confirm" is reported as is.
        if outcome != "finished" or report.overall != "fail" or round_ == MAX_REPAIRS:
            return "partial" if outcome == "finished" else "incomplete"
        s.ctx.emit("repair_started", {"round": round_ + 1})
        s.state.bump("repair_rounds")
        s.state.finished = False
        s.state.messages.append({"role": "user", "content": repair_message(report)})
        s.state.status = "running"
        outcome = executor.run()
    return "partial"


def execute(s: Session) -> int:
    """UNDERSTAND (if not done yet) → PLAN/EXECUTE/OBSERVE/ADAPT → VERIFY (→ REPAIR) → COMPLETE,
    unless the run pauses for a human. Always ends by writing the evidence report."""
    state, ctx, console = s.state, s.ctx, s.ui.console
    executor = Executor(ctx, s.llm, s.registry, checkpoint=lambda: s.store.save(state))
    try:
        state.status = "running"
        if state.contract is None:
            state.contract = understand(state.request, ctx, s.llm, s.registry)
            executor.start(state.contract)
            s.store.save(state)
        state.status = verify_and_repair(s, executor, executor.run())
    except RunPaused as pause:
        state.status = "awaiting_human"
        ctx.emit("run_paused", pause.request)
    except PlanRejected as exc:
        state.status = "failed"
        ctx.emit("run_error", {"error": f"plan rejected: {exc}"})
    except KeyboardInterrupt:
        state.status = "incomplete"
        ctx.emit("run_error", {"error": "interrupted by user"})
    except Exception as exc:  # report, don't hide: the run log keeps the traceback
        state.status = "failed"
        ctx.emit("run_error", {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})
        console.print(f"[red]Run failed: {type(exc).__name__}: {exc}[/red]")
    finally:
        ctx.close()
        if state.status != "awaiting_human":
            state.ended_at = now()
            ctx.emit("run_finished", {"status": state.status, "outcomes": len(state.outcomes), "steps": state.step,
                                      "tokens_in": state.tokens_in, "tokens_out": state.tokens_out})
        s.store.save(state)
        report = write_report(s.store.run_dir)
        s.ui.summary(state, str(report), s.llm.has_prices)
    return EXIT_CODES.get(state.status, 1)


def cmd_run(args: argparse.Namespace) -> int:
    console = Console()
    config = load_config()
    if args.headless:
        config.headless = True
    if args.max_steps:
        config.max_steps = args.max_steps
    if not config.agent_model or not (os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")):
        console.print("[red]Set OPENAI_API_KEY (or LLM_API_KEY) and AGENT_MODEL in .env first.[/red]")
        return 2

    run_id = new_run_id()
    state = RunState(run_id=run_id, request=args.request, human_mode=args.human)
    store = RunStore(config.runs_dir / run_id)
    s = open_session(config, state, store, console)
    s.ctx.emit("run_started", {"run_id": run_id, "request": args.request, "model": config.agent_model,
                               "run_dir": str(store.run_dir), "human": args.human})
    return execute(s)


def repair_after_pause(state: RunState, answer_text: str) -> None:
    """Make the saved conversation valid again and tell the model what happened while it waited."""
    if not state.messages:
        return  # paused while understanding the request: that stage simply runs again
    last_turn = max(i for i, m in enumerate(state.messages) if m["role"] == "assistant")
    answered = {m["tool_call_id"] for m in state.messages[last_turn:] if m["role"] == "tool"}
    for call in state.messages[last_turn].get("tool_calls") or []:
        if call["id"] not in answered:
            state.messages.append({"role": "tool", "tool_call_id": call["id"],
                                   "content": json.dumps({"ok": False, "error_type": "transient",
                                                          "message": PAUSED_RESULT})})
    state.messages.append({"role": "user", "content": (
        "The run was paused for a human and has now resumed. The browser was restarted, so you will need "
        f"to sign in again and navigate back. The human's response: {answer_text}. Continue the task; "
        "redo the paused action if it is still needed (the decision is on record and will be applied).")})


def cmd_resume(args: argparse.Namespace) -> int:
    console = Console()
    config = load_config()
    if args.headless:
        config.headless = True
    store = RunStore(config.runs_dir / args.run_id)
    if not store.state_path.exists():
        console.print(f"[red]No run {args.run_id} in {config.runs_dir}[/red]")
        return 2
    pending_path = store.run_dir / "pending.json"
    state = store.load()
    if state.status != "awaiting_human" or not pending_path.exists():
        console.print(f"[red]Run {args.run_id} is not waiting for a human (status: {state.status}).[/red]")
        return 2

    pending = json.loads(pending_path.read_text())
    if pending["kind"] == "approval":
        if args.approve == args.deny:
            console.print("[red]This run is waiting for an approval: pass --approve or --deny.[/red]")
            return 2
        response = HumanResponse(approved=args.approve, note=args.note or "")
        answer_text = ("approved" if args.approve else "denied") + (f" ({args.note})" if args.note else "")
    else:
        if not args.answer:
            console.print(f"[red]This run is waiting for an answer to: {pending['payload'].get('question')}\n"
                          "Pass --answer \"...\".[/red]")
            return 2
        response = HumanResponse(answer=args.answer, note=args.note or "")
        answer_text = args.answer

    state.human_responses[pending["key"]] = {"kind": pending["kind"], "rule_id": pending["payload"].get("rule_id"),
                                             "response": response.model_dump(), "consumed": False}
    pending_path.unlink()
    repair_after_pause(state, answer_text)
    s = open_session(config, state, store, console)
    # The decision is logged as approval_decided / question_answered when the paused step re-runs.
    s.ctx.emit("run_resumed", {"run_id": state.run_id, "kind": pending["kind"], "response": answer_text})
    return execute(s)


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-check a run against the systems as they are *now* (e.g. after someone changed a record)."""
    console = Console()
    config = load_config()
    config.headless = True
    store = RunStore(config.runs_dir / args.run_id)
    if not store.state_path.exists():
        console.print(f"[red]No run {args.run_id} in {config.runs_dir}[/red]")
        return 2
    state = store.load()
    if state.contract is None:
        console.print("[red]This run never got as far as a task contract; there is nothing to verify.[/red]")
        return 2
    s = open_session(config, state, store, console)
    try:
        report = verify(s.ctx, make_verifier_llm(config, state, s.ctx.emit))
    finally:
        s.ctx.close()
    state.status = {"pass": "completed", "fail": "partial"}.get(report.overall, "partial")
    s.ctx.emit("run_finished", {"status": state.status, "reverified": True})
    store.save(state)
    path = write_report(store.run_dir)
    s.ui.summary(state, str(path), s.llm.has_prices)
    return 0 if report.overall == "pass" else 1


def cmd_report(args: argparse.Namespace) -> int:
    config = load_config()
    run_dir = config.runs_dir / args.run_id
    if not (run_dir / "state.json").exists():
        Console().print(f"[red]No run {args.run_id} in {config.runs_dir}[/red]")
        return 2
    path = write_report(run_dir)
    Console().print(f"Report: {path}")
    if args.open:
        webbrowser.open(path.as_uri())
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    config = load_config()
    table = Table(title="Runs")
    for col in ("Run", "Status", "Outcomes", "Turns", "Request"):
        table.add_column(col)
    for path in sorted(config.runs_dir.glob("*/state.json"), reverse=True)[: args.limit]:
        state = RunStore(path.parent).load()
        table.add_row(state.run_id, state.status, str(len(state.outcomes)), str(state.step), state.request[:60])
    Console().print(table)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent", description="Autonomous company operator")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Carry out a request end to end")
    run.add_argument("request", help="What you want done, in plain language")
    run.add_argument("--headless", action="store_true", help="Hide the browser window")
    run.add_argument("--max-steps", type=int, help="Maximum model turns for the executor")
    run.add_argument("--human", choices=["terminal", "file", "auto-approve", "auto-deny"], default="terminal",
                     help="How approvals and questions reach a person: ask in this terminal, pause the run "
                          "and resume later (file), or answer automatically (for unattended runs)")
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="Answer a paused run's question or approval and continue it")
    resume.add_argument("run_id")
    decision = resume.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--deny", action="store_true")
    decision.add_argument("--answer", help="Answer to the operator's question")
    resume.add_argument("--note", help="Optional note for the record")
    resume.add_argument("--headless", action="store_true")
    resume.set_defaults(func=cmd_resume)

    verify_cmd = sub.add_parser("verify", help="Re-verify a run against the systems as they are now")
    verify_cmd.add_argument("run_id")
    verify_cmd.set_defaults(func=cmd_verify)

    report = sub.add_parser("report", help="(Re)generate a run's HTML evidence report")
    report.add_argument("run_id")
    report.add_argument("--open", action="store_true", help="Open it in the browser")
    report.set_defaults(func=cmd_report)

    runs = sub.add_parser("runs", help="List recent runs")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)

    args = parser.parse_args(argv)
    return args.func(args)
