"""The verifier: read-only tools that can still look around, a fresh context, and a verdict computed in code."""
import json
import sqlite3

from agent.models import AcceptanceCriterion, ClaimCheck, CriterionResult, Outcome, TaskContract, VerificationReport
from agent.tools import build_registry
from agent.verifier import repair_message, verify
from sandbox.server import sandbox_server
from tests.test_loop import FakeLLM

CONTRACT = TaskContract(
    request="r", objective="o", sop_used="invoice_processing", interpretation="i", inputs=[], assumptions=[],
    open_questions=[], expected_approvals=[], initial_plan=[],
    acceptance_criteria=[AcceptanceCriterion(id="AC1", description="d1", how_to_verify="h1"),
                         AcceptanceCriterion(id="AC2", description="d2", how_to_verify="h2")],
)


def report(statuses, claim="confirmed", overall="pass"):
    return VerificationReport(
        criteria=[CriterionResult(criterion_id=f"AC{i}", status=s, evidence=[], explanation=f"checked {i}")
                  for i, s in enumerate(statuses, 1)],
        claims=[ClaimCheck(item="doc-1", status=claim, explanation="looked")], overall=overall, summary="s")


def test_verdict_is_computed_not_trusted():
    assert report(["pass", "fail"], overall="pass").recompute_overall().overall == "fail"
    assert report(["pass", "pass"], claim="refuted").recompute_overall().overall == "fail"
    assert report(["pass", "unverifiable"]).recompute_overall().overall == "partial"
    assert report(["pass", "pass"], claim="unverifiable").recompute_overall().overall == "partial"
    assert report(["pass", "pass"], overall="fail").recompute_overall().overall == "pass"


def test_verifier_browser_works_while_the_operators_browser_is_open(ctx, events, tmp_path, monkeypatch):
    # Regression: a second Playwright driver in one process crashed every verifier browser call.
    ctx.state.contract = CONTRACT
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        assert build_registry().run(ctx, "browser_navigate", {"url": "/"}).ok  # operator's browser is open
        llm = FakeLLM([[("browser_sign_in", {})], [("browser_navigate", {"url": "/bills/BILL-0001"})], "done"],
                      reports=[report(["pass", "pass"])])
        verify(ctx, llm)
        results = [d for t, d in events if t == "tool_result" and d["tool"] in ("browser_sign_in", "browser_navigate")]
        assert [r["ok"] for r in results] == [True, True, True]
        assert not ctx.browser.logged_in  # the verifier signed in to its own, separate session


def test_verifier_sees_claims_but_never_the_operators_conversation(ctx, events):
    ctx.state.contract = CONTRACT
    ctx.state.outcomes = [Outcome(item="doc-1", status="created", details="record R-9")]
    ctx.state.messages = [{"role": "assistant", "content": "PRIVATE-OPERATOR-REASONING"}]
    llm = FakeLLM([[("list_handbook", {})], "done"], reports=[report(["pass", "fail"], overall="pass")])

    result = verify(ctx, llm)
    assert result.overall == "fail"  # recomputed: the model's "pass" is overridden
    shown = json.dumps(llm.seen)
    assert "PRIVATE-OPERATOR-REASONING" not in shown and "record R-9" in shown and "d2" in shown
    assert ctx.state.verifications[-1]["overall"] == "fail"
    assert [e for e, _ in events if e.startswith("verification")] == ["verification_started", "verification_result"]
    assert "AC2" in repair_message(result)


def test_verifier_observes_before_acting(ctx, events):
    # Regression: the verifier batched "go to page 2, go to page 3" and never looked at what it skipped past.
    ctx.state.contract = CONTRACT
    llm = FakeLLM([[("calculate", {"expression": "1+1"}), ("calculate", {"expression": "nope("}),
                    ("calculate", {"expression": "2+2"})], "done"], reports=[report(["pass", "pass"])])
    verify(ctx, llm)
    skipped = [d for t, d in events if t == "tool_skipped"]
    assert len(skipped) == 1 and skipped[0]["args"] == {"expression": "2+2"}


def test_read_only_tools_can_sign_in_search_and_follow_but_never_submit(ctx, tmp_path, monkeypatch):
    reg = build_registry().read_only()
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        signed_in = reg.run(ctx, "browser_sign_in", {})
        assert signed_in.ok and ctx.browser.logged_in

        assert reg.run(ctx, "browser_navigate", {"url": "/bills"}).ok
        assert reg.run(ctx, "browser_fill", {"label": "Search", "value": "INV-0987"}).ok
        searched = reg.run(ctx, "browser_follow", {"role": "button", "name": "Search"})  # a GET form
        assert searched.ok and "q=INV-0987" in searched.data["url"] and "BILL-0001" in searched.data["page"]
        assert reg.run(ctx, "browser_follow", {"name": "BILL-0001"}).data["url"].endswith("/bills/BILL-0001")

        reg.run(ctx, "browser_navigate", {"url": "/bills/new"})
        refused = reg.run(ctx, "browser_follow", {"role": "button", "name": "Create Bill"})  # a POST form
        assert refused.error_type == "policy_denied"
        assert "browser_click" not in reg.names()
        with sqlite3.connect(tmp_path / "acme.db") as db:
            assert db.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 3  # nothing changed
