"""Eval harness: run every scenario against a fresh sandbox and grade it from ground truth.

    python -m evals.run_evals [--only a,b] [--repeat N] [--model M] [--timeout MIN]

For each scenario the real CLI runs in a subprocess with the durable file channel. Whenever it
pauses for a human, the harness answers from the scenario's script and resumes it, the same way
a person would. The end state is then graded straight from the sandbox DB (ground_truth.py) and
compared with what the agent's own verifier concluded.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from evals import ground_truth
from sandbox.server import sandbox_server

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
OUTBOX = ROOT / "company" / "outbox"
MAX_PAUSES = 6
CHAOS_SEED = "42"
UNSCRIPTED_ANSWER = "No further information is available."
# Errors from the model provider, not the agent: a run cut short by one of these isn't the agent's fault.
PROVIDER_ERRORS = ("RateLimitError", "usage_limit", "insufficient_quota", "AuthenticationError",
                   "APIConnectionError", "PermissionDeniedError")
QUOTA_ERRORS = ("usage_limit", "insufficient_quota")


def load_scenarios(path: Path = EVALS / "scenarios.yaml") -> list[dict]:
    return yaml.safe_load(path.read_text())


def decide(pending: dict, human: dict, answers_used: int) -> tuple[list[str], bool]:
    """Resume arguments for a pending request, and whether the script covered it.
    Unscripted requests get the same safe defaults as the scripted channel: deny, or 'no info'."""
    if pending["kind"] == "approval":
        decision = (human.get("approvals") or {}).get(pending["payload"].get("rule_id"))
        flag = "--approve" if decision == "approve" else "--deny"
        return [flag, "--note", f"eval script: {decision or 'unscripted, denied'}"], decision is not None
    answers = human.get("answers") or []
    if answers_used < len(answers):
        return ["--answer", answers[answers_used]], True
    return ["--answer", UNSCRIPTED_ANSWER], False


def _agent(args: list[str], env: dict, log: Path, timeout_s: int) -> int | str:
    with log.open("a") as fh:
        fh.write(f"\n$ python -m agent {' '.join(args)}\n")
        fh.flush()
        try:
            return subprocess.run([sys.executable, "-m", "agent", *args], cwd=ROOT, env=env,
                                  stdout=fh, stderr=subprocess.STDOUT, timeout=timeout_s).returncode
        except subprocess.TimeoutExpired:
            return "timeout"


def _events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def run_metrics(run_dir: Path) -> dict:
    """What the run says about itself: status, its verifier's verdict, effort and cost."""
    state = json.loads((run_dir / "state.json").read_text())
    verifications = state.get("verifications") or []
    return {
        "status": state["status"],
        "verifier": verifications[-1]["overall"] if verifications else "none",
        "steps": state["step"],
        "recoveries": sum(v for k, v in state["counters"].items() if k.startswith("recovery:")),
        "repairs": state["counters"].get("repair_rounds", 0),
        "tokens_in": state["tokens_in"],
        "tokens_out": state["tokens_out"],
        "cost_usd": state["cost_usd"],
        "approvals": [e["data"].get("rule_id") for e in _events(run_dir) if e["type"] == "approval_decided"],
        "report": str((run_dir / "report.html").relative_to(ROOT)),
        "provider_error": next((e["data"]["error"] for e in _events(run_dir) if e["type"] == "run_error"
                                and any(m in e["data"].get("error", "") for m in PROVIDER_ERRORS)), None),
    }


def run_scenario(sc: dict, workdir: Path, model: str | None, timeout_s: int) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    log, runs_dir = workdir / "agent.log", workdir / "runs"
    human = sc.get("human") or {}
    result: dict = {"scenario": sc["name"], "pauses": [], "unscripted": [], "checks": []}
    started = time.monotonic()
    today = date.today()
    outbox_before = ground_truth.outbox_snapshot(OUTBOX)

    with sandbox_server(workdir, chaos=sc.get("chaos", ""), CHAOS_SEED=CHAOS_SEED) as base:
        db_path = workdir / "acme.db"
        env = {**os.environ, "ACME_ERP_URL": base, "RUNS_DIR": str(runs_dir), "HEADLESS": "1"}
        if model:
            env["AGENT_MODEL"] = env["VERIFIER_MODEL"] = model
        code = _agent(["run", sc["request"], "--human", "file", "--headless"], env, log, timeout_s)
        run_dirs = sorted(runs_dir.glob("*/state.json"))
        run_dir = run_dirs[0].parent if run_dirs else None
        answers_used = 0
        while code == 3 and run_dir and len(result["pauses"]) < MAX_PAUSES:
            pending = json.loads((run_dir / "pending.json").read_text())
            args, scripted = decide(pending, human, answers_used)
            answers_used += pending["kind"] == "question"
            payload = pending["payload"]
            pause = {"kind": pending["kind"], "rule_id": payload.get("rule_id"),
                     "question": payload.get("question"), "response": args[1] if args[0] == "--answer" else args[0],
                     "records_written_before": ground_truth.new_records(db_path)}
            result["pauses"].append(pause)
            if not scripted:
                result["unscripted"].append(pause)
            code = _agent(["resume", run_dir.name, *args, "--headless"], env, log, timeout_s)

        result["exit"] = code
        result["seconds"] = round(time.monotonic() - started)
        result["checks"] = [vars(c) for c in grade(sc, db_path, today, outbox_before, result["pauses"])]
    if run_dir:
        result |= run_metrics(run_dir)
        result["checks"] += [vars(c) for c in approval_checks(sc, result["approvals"])]
    else:
        result |= {"status": "no run", "verifier": "none"}
    result["ground_truth"] = bool(result["checks"]) and all(c["passed"] for c in result["checks"])
    result["agrees"] = result["ground_truth"] == (result["verifier"] == "pass")
    if result.get("provider_error"):
        # The provider stopped the run. A correct end state is still evidence; an incorrect one can't be
        # blamed on the agent, and the verifier never got to finish, so agreement isn't measured.
        result["ground_truth"] = True if result["ground_truth"] else None
        result["agrees"] = None
    return result


def grade(sc: dict, db_path: Path, today: date, outbox_before: dict, pauses: list[dict]) -> list[ground_truth.Check]:
    checks = ground_truth.CHECKS[sc["check"]](db_path=db_path, today=today, outbox=OUTBOX, outbox_before=outbox_before)
    if sc.get("question_first"):
        first = pauses[0] if pauses else None
        ok = bool(first) and first["kind"] == "question" and not any(first["records_written_before"].values())
        checks.insert(0, ground_truth.Check("asked before writing anything", ok,
                                            str(first and {k: first[k] for k in ("kind", "records_written_before")})))
    return checks


def approval_checks(sc: dict, decided: list[str]) -> list[ground_truth.Check]:
    return [ground_truth.Check(f"{rule} decided exactly once", decided.count(rule) == 1, f"decided: {decided}")
            for rule in sc.get("expect_approvals") or []]


# --- output ---------------------------------------------------------------------------------

def _fmt_tokens(r: dict) -> str:
    return f"{r.get('tokens_in', 0) / 1e6:.2f}M / {r.get('tokens_out', 0) / 1e3:.0f}k"


def _mark(value: bool | None) -> str:
    return "—" if value is None else ("✅" if value else "❌")


def to_markdown(results: list[dict], model: str, sha: str) -> str:
    graded = [r for r in results if r["ground_truth"] is not None]
    compared = [r for r in results if r["agrees"] is not None]
    passed = sum(r["ground_truth"] for r in graded)
    agreed = sum(r["agrees"] for r in compared)
    false_pass = [r for r in compared if r["verifier"] == "pass" and not r["ground_truth"]]
    ungraded = len(results) - len(graded)
    has_cost = any(r.get("cost_usd") for r in results)
    lines = [
        "# Eval results", "",
        f"{datetime.now():%Y-%m-%d %H:%M} · model `{model}` · commit `{sha}` · chaos seed {CHAOS_SEED}", "",
        f"**Ground truth: {passed}/{len(graded)} scenarios correct · verifier agreed with ground truth "
        f"{agreed}/{len(compared)} · false passes: {len(false_pass)}**"
        + (f" · {ungraded} not graded (model provider error)" if ungraded else ""), "",
        "Ground truth is read straight from the sandbox database and the outbox, independently of the agent "
        "and of its verifier. *Agrees* means the verifier's verdict (pass vs not pass) matched it; a "
        "*false pass* (verifier passed, ground truth failed) is the dangerous kind of disagreement.", "",
        "| scenario | ground truth | run status | verifier said | agrees? | steps | recoveries | repairs "
        "| human | tokens in/out | cost | time |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        checks = r["checks"]
        truth = (f"{_mark(r['ground_truth'])} {sum(c['passed'] for c in checks)}/{len(checks)}"
                 if r["ground_truth"] is not None else "⚠️ not graded")
        cost = f"${r['cost_usd']:.2f}" if has_cost else "—"
        lines.append(
            f"| {r['scenario']} | {truth} | {r['status']} | {r['verifier']} | {_mark(r['agrees'])} "
            f"| {r.get('steps', '—')} | {r.get('recoveries', '—')} | {r.get('repairs', '—')} | {len(r['pauses'])} "
            f"| {_fmt_tokens(r)} | {cost} | {r['seconds'] // 60}m{r['seconds'] % 60:02d}s |")

    problems = [r for r in results if not r["ground_truth"] or r["agrees"] is False or r["unscripted"]
                or r.get("provider_error")]
    lines += ["", "## Failures and findings", ""]
    if not problems:
        lines.append("None.")
    for r in problems:
        lines.append(f"### {r['scenario']}")
        if r.get("provider_error"):
            lines.append(f"- ⚠️ stopped by the model provider: `{r['provider_error'][:160]}`")
        else:
            lines += [f"- ❌ {c['name']}: {c['detail']}" for c in r["checks"] if not c["passed"]]
        if r["agrees"] is False:
            kind = "false pass" if r["verifier"] == "pass" else "false alarm"
            lines.append(f"- verifier disagreed with ground truth ({kind}: verifier said `{r['verifier']}`)")
        lines += [f"- unscripted human request ({p['kind']} {p['rule_id'] or ''}): {p['question'] or ''}"
                  for p in r["unscripted"]]
        if r.get("report"):
            lines.append(f"- report: `{r['report']}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run_evals", description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="Comma-separated scenario names")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", help="Override AGENT_MODEL and VERIFIER_MODEL for this suite")
    parser.add_argument("--timeout", type=int, default=20, help="Minutes per agent process")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env")

    scenarios = load_scenarios()
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {s["name"] for s in scenarios}
        if unknown:
            parser.error(f"unknown scenarios: {', '.join(sorted(unknown))}")
        scenarios = [s for s in scenarios if s["name"] in wanted]

    model = args.model or os.environ.get("AGENT_MODEL", "")
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    session = ROOT / "runs" / "evals" / datetime.now().strftime("%Y%m%d-%H%M%S")
    results = []
    for sc in scenarios:
        for i in range(args.repeat):
            name = sc["name"] + (f"-{i + 1}" if args.repeat > 1 else "")
            print(f"▶ {name} …", flush=True)
            try:
                r = run_scenario(sc, session / name, args.model, args.timeout * 60)
            except Exception as exc:  # one broken scenario must not lose the rest of the suite
                r = {"scenario": name, "status": f"harness error: {type(exc).__name__}: {exc}", "verifier": "none",
                     "pauses": [], "unscripted": [], "seconds": 0, "ground_truth": False, "agrees": True,
                     "checks": [{"name": "harness", "passed": False, "detail": str(exc)}]}
            r["scenario"] = name
            results.append(r)
            truth = {True: "PASS", False: "FAIL", None: "NOT GRADED"}[r["ground_truth"]]
            print(f"  ground truth {truth} · verifier {r['verifier']} · status {r['status']} · {r['seconds']}s", flush=True)
            # written after every scenario, so a long suite leaves usable results if interrupted
            (session / "results.json").write_text(json.dumps(results, indent=2, default=str))
            (EVALS / "results.md").write_text(to_markdown(results, model, sha))
            if any(m in (r.get("provider_error") or "") for m in QUOTA_ERRORS):
                print(f"  ⚠️ model quota exhausted, stopping the suite: {r['provider_error'][:160]}", flush=True)
                break
        else:
            continue
        break
    (EVALS / "results.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults: {EVALS / 'results.md'}")
    return 0 if all(r["ground_truth"] is True for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
