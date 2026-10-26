"""Live terminal view of a run, driven entirely by the event stream."""
from __future__ import annotations

from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent.models import RunState

STEP_ICONS = {"pending": "○", "in_progress": "◐", "done": "●", "failed": "✗", "skipped": "–"}


def _short(value, limit: int = 70) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _params(payload: dict) -> str:
    return "  ".join(f"{k}={v}" for k, v in (payload.get("params") or {}).items())


def describe_call(tool: str, args: dict) -> str:
    if tool == "browser_click":
        return f'{args.get("role")} "{args.get("name")}"'
    if tool in ("browser_fill", "browser_select"):
        return f'{args.get("label")} = {args.get("value", args.get("option"))}'
    if tool == "browser_navigate":
        return str(args.get("url"))
    if "path" in args:
        return str(args["path"]).rsplit("/", 1)[-1]
    if tool == "record_outcome":
        return f'{args.get("item")} → {args.get("status")}'
    if tool == "update_plan":
        return f'{len(args.get("steps", []))} steps'
    return _short(", ".join(f"{k}={v}" for k, v in args.items()), 60)


class ConsoleUI:
    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self._pending: dict | None = None

    def on_event(self, event_type: str, data: dict) -> None:
        handler = getattr(self, f"_on_{event_type}", None)
        if handler:
            handler(data)

    def _on_run_started(self, d: dict) -> None:
        self.console.rule(f"[bold]Operator run {d['run_id']}[/bold]")
        self.console.print(f"[bold]Request:[/bold] {d['request']}\n[dim]model {d['model']} · run dir {d['run_dir']}[/dim]")

    def _on_contract_created(self, c: dict) -> None:
        lines = [f"[bold]Objective:[/bold] {c['objective']}",
                 f"[bold]Procedure:[/bold] {c['sop_used'] or 'none (plan needs approval)'}",
                 f"[bold]Interpretation:[/bold] {c['interpretation']}"]
        if c["assumptions"]:
            lines.append("[bold]Assumptions:[/bold] " + "; ".join(c["assumptions"]))
        lines.append("[bold]Done when:[/bold]")
        lines += [f"  {a['id']}. {a['description']}" for a in c["acceptance_criteria"]]
        if c["expected_approvals"]:
            lines.append(f"[bold]Approvals expected:[/bold] {', '.join(c['expected_approvals'])}")
        self.console.print(Panel("\n".join(lines), title="📋 Here's what I understood", border_style="blue"))

    def _on_plan_updated(self, d: dict) -> None:
        steps = "  ".join(f"{STEP_ICONS.get(s['status'], '?')} {_short(s['title'], 40)}" for s in d["steps"])
        self.console.print(f"[blue]🗒  plan:[/blue] {steps}")

    def _on_tool_call(self, d: dict) -> None:
        self._pending = d

    def _on_tool_result(self, d: dict) -> None:
        call = self._pending or {"tool": d["tool"], "args": {}}
        self._pending = None
        if d["tool"] in ("update_plan",):
            return  # shown by plan_updated
        head = f"🔧 {d['tool']}({_short(describe_call(d['tool'], call.get('args', {})))})"
        if d["ok"]:
            url = urlparse(d.get("url", ""))
            where = url.path + (f"?{url.query}" if url.query else "")
            extra = f" [green]{'; '.join(d['notices'])}[/green]" if d.get("notices") else ""
            self.console.print(f"{head} → ✅ {where}{extra}")
        else:
            colour = "red" if d["error_type"] in ("fatal", "policy_denied") else "yellow"
            self.console.print(f"{head} → [{colour}]❌ {d['error_type']}: {_short(d['message'], 110)}[/{colour}]")

    def _on_tool_skipped(self, d: dict) -> None:
        self.console.print(f"[dim]⏭  {d['tool']}({_short(describe_call(d['tool'], d['args']))}) skipped[/dim]")

    def _on_outcome_recorded(self, d: dict) -> None:
        self.console.print(f"[bold]📌 {d['item']}[/bold] → {d['status']}: [dim]{_short(d['details'], 100)}[/dim]")

    def _on_recovery(self, d: dict) -> None:
        if d["type"] in ("reflection", "escalated"):
            label = "🧭 steering the operator" if d["type"] == "reflection" else "🆘 escalating to a human"
            self.console.print(f"[bold yellow]{label}: {_short(d.get('reason', ''), 120)}[/bold yellow]")
            return
        detail = {"element_relocated": f"{d.get('from')} → {d.get('to')}"}.get(d["type"], d.get("url", ""))
        self.console.print(f"[yellow]♻  recovery: {d['type']} {_short(detail, 70)}[/yellow]")

    def _on_approval_requested(self, d: dict) -> None:
        self.console.print(f"[magenta]⏸  approval needed ({d.get('rule_id', '')}): {d['summary']} "
                           f"[{_short(_params(d), 90)}][/magenta]")

    def _on_approval_decided(self, d: dict) -> None:
        verdict = "[green]approved[/green]" if d["approved"] else "[red]denied[/red]"
        self.console.print(f"[magenta]👤 {d.get('rule_id', '')} {verdict}[/magenta] {d.get('note', '')}")

    def _on_policy_check(self, d: dict) -> None:
        if d["decision"] == "deny":
            self.console.print(f"[red]⛔ policy {d.get('rule')} blocked {d['action']}[/red]")
        elif d["decision"] == "check_failed":
            self.console.print(f"[red]⛔ check {d.get('rule')} blocked {d['action']}: {d.get('detail')}[/red]")
        elif d["decision"] == "duplicate_blocked":
            self.console.print(f"[red]⛔ duplicate {d['action']} blocked (already done: {d.get('previous')})[/red]")

    def _on_run_paused(self, d: dict) -> None:
        what = d["payload"].get("summary") or d["payload"].get("question", "")
        rule = f" under {d['payload']['rule_id']}" if d["payload"].get("rule_id") else ""
        self.console.print(Panel(f"{what}{rule}\n{_params(d['payload'])}".rstrip(),
                                 title="⏸  Paused: waiting for a human", border_style="magenta"))

    def _on_run_resumed(self, d: dict) -> None:
        self.console.rule(f"[magenta]▶ Run {d['run_id']} resumed: human said {d['response']}[/magenta]")

    def _on_question_answered(self, d: dict) -> None:
        self.console.print(f"[cyan]💬 answer: {_short(d['answer'], 100)}[/cyan]")

    def _on_assistant_note(self, d: dict) -> None:
        self.console.print(f"[dim italic]💭 {_short(d['text'], 140)}[/dim italic]")

    def _on_verification_started(self, d: dict) -> None:
        self.console.rule(f"[bold cyan]🔎 Independent verification (round {d['round']}): fresh context, "
                          f"read-only tools[/bold cyan]")

    def _on_verification_result(self, d: dict) -> None:
        colour = {"pass": "green", "fail": "red"}.get(d["overall"], "yellow")
        icons = {"pass": "✅", "fail": "❌", "unverifiable": "❔"}
        for c in d["criteria"]:
            self.console.print(f"  {icons[c['status']]} {c['criterion_id']}: {_short(c['explanation'], 130)}")
        for c in d["claims"]:
            if c["status"] != "confirmed":
                colour = "red" if c["status"] == "refuted" else "yellow"
                self.console.print(f"  [{colour}]✗ claim {c['status']}: {c['item']}: {_short(c['explanation'], 110)}[/{colour}]")
        self.console.print(f"[bold {colour}]Verification: {d['overall'].upper()}[/bold {colour}]: {_short(d['summary'], 160)}")

    def _on_repair_started(self, d: dict) -> None:
        self.console.rule(f"[bold yellow]🔧 Repair round {d['round']}: sending the verifier's findings back to the operator[/bold yellow]")

    def _on_llm_fallback(self, d: dict) -> None:
        self.console.print(f"[dim]⚙  {d['model']} rejected reasoning_effort={d['reasoning_effort']} with tools; "
                           f"using {d['now'] or 'no setting'}[/dim]")

    def _on_budget_exceeded(self, d: dict) -> None:
        self.console.print(f"[red]⏹  {d['kind']} budget of {d['limit']} reached[/red]")

    def _on_stuck(self, d: dict) -> None:
        self.console.print("[red]⏹  the operator stopped making progress[/red]")

    def summary(self, state: RunState, run_dir: str, has_prices: bool) -> None:
        table = Table(title="Outcomes", show_lines=False)
        for col in ("Item", "Outcome", "Details"):
            table.add_column(col)
        for o in state.outcomes:
            table.add_row(o.item, o.status, _short(o.details, 80))
        self.console.print(table)
        if state.status == "awaiting_human":
            self.console.print(f"[magenta]⏸  Waiting for a human. Answer with:[/magenta]\n"
                               f"   python -m agent resume {state.run_id} --approve | --deny  [--note \"…\"]\n"
                               f"   python -m agent resume {state.run_id} --answer \"…\"   (if it asked a question)")
        recoveries = {k.split(":", 1)[1]: v for k, v in state.counters.items() if k.startswith("recovery:")}
        if recoveries:
            parts = ", ".join(f"{v} {k}" for k, v in sorted(recoveries.items(), key=lambda kv: -kv[1]))
            self.console.print(f"[yellow]♻  {sum(recoveries.values())} recoveries: {parts}[/yellow]")

        counts: dict[str, int] = {}
        for o in state.outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        approvals = state.counters.get("human_approvals", 0)
        line = " · ".join(f"{n} {status}" for status, n in counts.items())
        if approvals:
            line += f" · 👤 {approvals} approval{'s' if approvals > 1 else ''}"
        if line:
            self.console.print(f"📦 {line}")

        if state.verifications:
            last = state.verifications[-1]
            passed = sum(c["status"] == "pass" for c in last["criteria"])
            verified = f"Verified {passed}/{len(last['criteria'])} criteria"
            if repairs := state.counters.get("repair_rounds"):
                verified += f" after {repairs} repair round{'s' if repairs > 1 else ''}"
        else:
            verified = "Not verified"
        cost = f"${state.cost_usd:.3f}" if has_prices else "cost n/a (set PRICE_* in .env)"
        colour = {"completed": "green", "partial": "yellow", "awaiting_human": "magenta"}.get(state.status, "red")
        self.console.print(f"[bold {colour}]{state.status.upper()}[/bold {colour}] · {verified} · "
                           f"{sum(recoveries.values())} recoveries · {state.step} turns · {state.llm_calls} model calls · "
                           f"{state.tokens_in:,} in / {state.tokens_out:,} out tokens · {cost}")
        self.console.print(f"[dim]Evidence report: {run_dir}[/dim]")
