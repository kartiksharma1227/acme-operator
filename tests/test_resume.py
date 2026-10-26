"""Durable pause/resume end to end: real CLI, real sandbox, real browser, scripted model (no API cost).

The run pauses at a policy approval and the process exits. Later, `resume --approve` (or
`--deny`) continues it in a fresh process with a fresh browser, and the decision is applied
when the model redoes the paused submission.
"""
import json
import sqlite3

import pytest

from agent import cli
from agent.models import AcceptanceCriterion, TaskContract
from agent.state import RunStore
from sandbox.server import sandbox_server
from tests.test_loop import FakeLLM
from tests.test_verifier import report

CONTRACT = TaskContract(
    request="enter the large one", objective="Enter the large record", sop_used="invoice_processing",
    interpretation="test", inputs=[], assumptions=[], open_questions=[], expected_approvals=["FIN-3"],
    acceptance_criteria=[AcceptanceCriterion(id="AC1", description="entered", how_to_verify="look")],
    initial_plan=["enter it"],
)
SIGN_IN = [
    [("browser_navigate", {"url": "/bills/new"})],
    [("get_credentials", {"system": "acme_erp"})],
    [("browser_fill", {"label": "Username", "value": "agent"}),
     ("browser_fill", {"label": "Password", "value": "$secret:acme_erp.password"}),
     ("browser_click", {"role": "button", "name": "Sign in"})],
]
SUBMIT_LARGE = [[
    ("browser_select", {"label": "Vendor", "option": "Initech Software"}),
    ("browser_fill", {"label": "PO Number", "value": "PO-1043"}),
    ("browser_fill", {"label": "Invoice Number", "value": "INV-1002"}),
    ("browser_fill", {"label": "Invoice Date", "value": "2026-10-20"}),
    ("browser_fill", {"label": "Amount", "value": "12800.00"}),
    ("browser_fill", {"label": "GL Code", "value": "6400"}),
    ("browser_click", {"role": "button", "name": "Create Bill"}),
]]


def finish(status):
    return [[("record_outcome", {"item": "INV-1002", "status": status, "details": "-"}),
             ("finish", {"summary": "done"})]]


@pytest.fixture
def env(tmp_path, monkeypatch):
    # These tests are about pausing; the verifier is scripted to agree so runs can complete.
    monkeypatch.setattr(cli, "make_verifier_llm", lambda config, state, emit: FakeLLM(["done"], reports=[report(["pass"])]))
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("HEADLESS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    return tmp_path


def script(monkeypatch, turns):
    # understand(): one turn without tool calls, then the canned contract
    monkeypatch.setattr(cli, "make_llm", lambda config, state, emit: FakeLLM(["understood", *turns], CONTRACT))


def bills_for(db_path, invoice):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT amount FROM bills WHERE invoice_number = ?", (invoice,)).fetchall()


@pytest.mark.parametrize("decision,expected_bills,expected_status", [("--approve", [(12800.0,)], "completed"),
                                                                     ("--deny", [], "completed")])
def test_pause_then_resume(env, monkeypatch, decision, expected_bills, expected_status):
    with sandbox_server(env) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)

        script(monkeypatch, [*SIGN_IN, *SUBMIT_LARGE])
        assert cli.main(["run", "enter the large one", "--human", "file"]) == 3
        (run_dir,) = (env / "runs").iterdir()
        store = RunStore(run_dir)
        assert store.load().status == "awaiting_human"
        pending = json.loads((run_dir / "pending.json").read_text())
        assert pending["payload"]["rule_id"] == "FIN-3"
        assert bills_for(env / "acme.db", "INV-1002") == []  # nothing submitted while waiting

        # A new process, a new browser: the model signs in again and redoes the submission.
        outcome = "created" if decision == "--approve" else "not entered: approval denied"
        script(monkeypatch, [*SIGN_IN, *SUBMIT_LARGE, *finish(outcome)])
        assert cli.main(["resume", run_dir.name, decision, "--note", "checked"]) == 0

        state = store.load()
        assert state.status == expected_status and not (run_dir / "pending.json").exists()
        assert bills_for(env / "acme.db", "INV-1002") == expected_bills
        assert len(state.action_ledger) == len(expected_bills)
        events = [e["type"] for e in store.events()]
        assert events.count("run_paused") == 1 and "run_resumed" in events
        decided = [e["data"] for e in store.events() if e["type"] == "approval_decided"]
        assert [d["approved"] for d in decided] == [decision == "--approve"]
        # the paused call got a result, so the conversation stayed valid for the API
        asked = [c["id"] for m in state.messages if m["role"] == "assistant" for c in m.get("tool_calls") or []]
        assert asked == [m["tool_call_id"] for m in state.messages if m["role"] == "tool"]


def test_task_without_procedure_waits_for_plan_approval(env, monkeypatch):
    no_sop = CONTRACT.model_copy(update={"sop_used": None})
    monkeypatch.setattr(cli, "make_llm", lambda config, state, emit: FakeLLM(["understood"], no_sop))
    assert cli.main(["run", "something new", "--human", "file"]) == 3
    (run_dir,) = (env / "runs").iterdir()
    assert json.loads((run_dir / "pending.json").read_text())["payload"]["rule_id"] == "unknown_task"
    assert RunStore(run_dir).load().contract is None  # paused before any action

    # On resume, understanding continues from the saved draft (no new model call), finds the
    # approval on record, and goes straight to work.
    resumed = FakeLLM([[("finish", {"summary": "nothing to do"})]], contract=None)
    monkeypatch.setattr(cli, "make_llm", lambda config, state, emit: resumed)
    assert cli.main(["resume", run_dir.name, "--approve"]) == 0
    state = RunStore(run_dir).load()
    assert state.status == "completed"
    assert any("approved this plan" in a for a in state.contract.assumptions)  # the executor is told
    assert len(resumed.seen) == 1  # only the executor's turn: the contract was not regenerated


def test_missing_details_are_asked_before_anything_else_and_resume_from_the_draft(env, monkeypatch):
    from agent.models import OpenQuestion
    vague = CONTRACT.model_copy(update={"open_questions": [
        OpenQuestion(question="Which department?", blocking=True),
        OpenQuestion(question="Please approve the plan", blocking=True),  # never asked as a question
    ]})
    monkeypatch.setattr(cli, "make_llm", lambda config, state, emit: FakeLLM(["understood"], vague))
    assert cli.main(["run", "do the thing", "--human", "file"]) == 3
    (run_dir,) = (env / "runs").iterdir()
    pending = json.loads((run_dir / "pending.json").read_text())
    assert pending["kind"] == "question" and "Which department?" in pending["payload"]["question"]
    assert "approve" not in pending["payload"]["question"].lower()
    assert RunStore(run_dir).load().draft_contract is not None

    answered = FakeLLM([[("finish", {"summary": "done"})]], contract=CONTRACT)  # the revised draft
    monkeypatch.setattr(cli, "make_llm", lambda config, state, emit: answered)
    assert cli.main(["resume", run_dir.name, "--answer", "Sales"]) == 0
    revision = json.dumps(answered.seen[0])
    assert "Sales" in revision and "Which department?" in revision  # revised from the saved draft


def test_resume_refuses_a_run_that_is_not_waiting(env):
    assert cli.main(["resume", "nope", "--approve"]) == 2
