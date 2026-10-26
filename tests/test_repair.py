"""VERIFY → REPAIR → VERIFY through the real run flow, with scripted models (no API cost)."""
from rich.console import Console

from agent import cli
from agent.config import load_config
from agent.models import RunState
from agent.state import RunStore
from tests.test_loop import FakeLLM
from tests.test_verifier import CONTRACT, report


def finish_turn(details):
    return [("record_outcome", {"item": "doc-1", "status": "created", "details": details}),
            ("finish", {"summary": details})]


def run_with(tmp_path, monkeypatch, operator_turns, verdicts):
    monkeypatch.setattr(cli, "make_llm", lambda config, state, emit: FakeLLM(operator_turns))
    monkeypatch.setattr(cli, "make_verifier_llm",
                        lambda config, state, emit: FakeLLM(["done"] * len(verdicts), reports=verdicts))
    config = load_config()
    state = RunState(run_id="t", request="r", contract=CONTRACT, human_mode="auto-approve",
                     messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    store = RunStore(tmp_path / "run")
    code = cli.execute(cli.open_session(config, state, store, Console(quiet=True)))
    return code, store.load()


def test_failed_verification_is_repaired_then_passes(tmp_path, monkeypatch):
    code, state = run_with(tmp_path, monkeypatch,
                           [finish_turn("first try"), finish_turn("fixed")],
                           [report(["pass", "fail"]), report(["pass", "pass"])])
    assert code == 0 and state.status == "completed"
    assert [v["overall"] for v in state.verifications] == ["fail", "pass"]
    repair = [m["content"] for m in state.messages if m["role"] == "user" and "independent verifier" in m["content"]]
    assert len(repair) == 1 and "AC2" in repair[0]
    assert (tmp_path / "run" / "report.html").exists()


def test_unverifiable_is_reported_not_repaired(tmp_path, monkeypatch):
    code, state = run_with(tmp_path, monkeypatch, [finish_turn("done")], [report(["pass", "unverifiable"])])
    assert code == 1 and state.status == "partial" and len(state.verifications) == 1
    assert not any("independent verifier" in (m.get("content") or "") for m in state.messages)


def test_never_passing_ends_partial_after_two_repairs(tmp_path, monkeypatch):
    code, state = run_with(tmp_path, monkeypatch, [finish_turn("a"), finish_turn("b"), finish_turn("c")],
                           [report(["fail", "pass"])] * 3)
    assert code == 1 and state.status == "partial" and len(state.verifications) == 3
