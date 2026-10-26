"""Executor loop mechanics with a scripted fake model: no API calls, fully deterministic."""
import json

from agent.llm import LLMResponse, ToolCall
from agent.loop import Executor
from agent.state import RunStore


class FakeLLM:
    """Replays scripted turns. A turn is either text (no tool calls) or a list of (tool, args)."""
    has_prices = False

    def __init__(self, turns, contract=None, reports=None):
        self.turns = list(turns)
        self.calls = 0
        self.contract = contract
        self.reports = list(reports or [])  # VerificationReports, returned in order
        self.seen: list[list[dict]] = []    # every conversation the model was shown

    def structured(self, messages, model_cls):
        self.seen.append(messages)
        return self.contract if model_cls.__name__ == "TaskContract" else self.reports.pop(0)

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        self.calls += 1
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            return LLMResponse(turn, [], {"role": "assistant", "content": turn})
        calls = [ToolCall(f"t{self.calls}-{i}", name, json.dumps(args)) for i, (name, args) in enumerate(turn)]
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls]}
        return LLMResponse(None, calls, message)


def executor(ctx, registry, turns, checkpoint=lambda: None):
    ctx.state.messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do it"}]
    return Executor(ctx, FakeLLM(turns), registry, checkpoint=checkpoint)


def tool_messages(ctx):
    return [m for m in ctx.state.messages if m["role"] == "tool"]


def assert_paired(ctx):
    """Every tool call the model made has exactly one result message (the API requires it)."""
    asked = [c["id"] for m in ctx.state.messages if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in tool_messages(ctx)]
    assert asked == answered


def test_runs_tools_until_finish(ctx, registry):
    ex = executor(ctx, registry, [
        [("update_plan", {"steps": [{"id": "1", "title": "look", "status": "in_progress"}]})],
        [("list_handbook", {})],
        [("record_outcome", {"item": "a", "status": "done", "details": "ok"}), ("finish", {"summary": "done"})],
    ])
    assert ex.run() == "finished"
    assert ctx.state.step == 3 and ctx.state.finished and ctx.state.outcomes[0].item == "a"
    assert_paired(ctx)


def test_failure_skips_rest_of_batch(ctx, registry, events):
    ex = executor(ctx, registry, [
        [("read_pdf", {"path": "missing.pdf"}), ("list_handbook", {}), ("finish", {"summary": "too early"})],
        [("finish", {"summary": "now"})],
    ])
    assert ex.run() == "finished"
    first_turn = [json.loads(m["content"]) for m in tool_messages(ctx)[:3]]
    assert first_turn[0]["error_type"] == "validation"
    assert all(r["message"].startswith("Skipped: an earlier action") for r in first_turn[1:])
    assert ctx.state.step == 2  # the premature finish was skipped, so a second turn was needed
    assert sum(e == "tool_skipped" for e, _ in events) == 2
    assert_paired(ctx)


def test_only_one_page_change_per_turn(ctx, registry):
    from agent.models import ToolResult
    from agent.tools import Tool, ToolRegistry

    class Open(Tool):
        name = "open_view"
        description = "pretend to load a page"
        parameters = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        changes_view = True

        def run(self, ctx, n):
            return ToolResult.success({"opened": n})

    reg = ToolRegistry([Open(), *registry.tools.values()])
    ex = Executor(ctx, FakeLLM([[("open_view", {"n": 1}), ("open_view", {"n": 2}), ("list_handbook", {})],
                                [("finish", {"summary": "done"})]]), reg)
    ctx.state.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    ex.run()
    results = [json.loads(m["content"]) for m in tool_messages(ctx)[:3]]
    assert results[0]["ok"] and results[0]["data"] == {"opened": 1}
    assert all("changed the page" in r["message"] for r in results[1:])
    assert_paired(ctx)


def test_repeating_a_failing_call_gets_steered(ctx, registry, events):
    bad = [("read_pdf", {"path": "missing.pdf"})]
    ex = executor(ctx, registry, [bad, bad, bad, [("finish", {"summary": "gave up honestly"})]])
    assert ex.run() == "finished"
    steering = [m["content"] for m in ctx.state.messages if m["role"] == "user" and "stuck" in m["content"]]
    assert len(steering) == 1 and "failed 3 times" in steering[0]
    assert any(e == "recovery" and d["type"] == "reflection" for e, d in events)


def test_calls_after_finish_are_skipped(ctx, registry):
    ex = executor(ctx, registry, [[("finish", {"summary": "done"}), ("list_handbook", {})]])
    assert ex.run() == "finished"
    assert "finish was already called" in tool_messages(ctx)[1]["content"]
    assert_paired(ctx)


def test_nudges_then_stuck(ctx, registry):
    ex = executor(ctx, registry, ["thinking", "still thinking", "hmm"])
    assert ex.run() == "stuck"
    nudges = [m for m in ctx.state.messages if m["role"] == "user" and "Continue the task" in m["content"]]
    assert len(nudges) == 2


def test_step_budget(ctx, registry, events):
    ctx.config.max_steps = 2
    ex = executor(ctx, registry, [[("list_handbook", {})]] * 5)
    assert ex.run() == "budget" and ctx.state.step == 2
    assert ("budget_exceeded", {"kind": "steps", "limit": 2}) in events


def test_state_is_checkpointed(ctx, registry, tmp_path):
    store = RunStore(tmp_path / "run")
    ex = executor(ctx, registry, [[("list_handbook", {})], [("finish", {"summary": "done"})]],
                  checkpoint=lambda: store.save(ctx.state))
    ex.run()
    saved = store.load()
    assert saved.finished and saved.step == 2 and len(saved.messages) == len(ctx.state.messages)


def test_model_endpoint_can_be_redirected_without_touching_the_openai_key(monkeypatch):
    from agent.llm import LLM
    from agent.models import RunState

    monkeypatch.setenv("OPENAI_API_KEY", "real-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:8317/v1")
    monkeypatch.setenv("LLM_API_KEY", "proxy-key")
    client = LLM("m", RunState(run_id="t"), lambda *_: None).client
    assert str(client.base_url).startswith("http://127.0.0.1:8317/v1") and client.api_key == "proxy-key"

    monkeypatch.delenv("LLM_BASE_URL")
    monkeypatch.delenv("LLM_API_KEY")
    client = LLM("m", RunState(run_id="t"), lambda *_: None).client
    assert "api.openai.com" in str(client.base_url) and client.api_key == "real-key"
