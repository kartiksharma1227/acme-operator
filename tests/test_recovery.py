"""Runtime-level recovery: stuck/stall detection, reflection, escalation, and recognising fixes."""
from types import SimpleNamespace

from agent.human import ScriptedChannel
from agent.models import Outcome, RunState, ToolResult
from agent.recovery import REPEAT_LIMIT, STALL_TURNS, RecoveryMonitor, note_recovery

FAIL = ToolResult.fail("not_found", "no such control")
OK = ToolResult.success({})


def monitor(answers=None):
    events = []
    ctx = SimpleNamespace(state=RunState(run_id="t"), emit=lambda t, d: events.append((t, d)),
                          human=ScriptedChannel(answers=answers or ["skip it"]), _browser=None)
    return RecoveryMonitor(ctx), ctx, events


def recoveries(events, kind):
    return [d for t, d in events if t == "recovery" and d["type"] == kind]


def test_repeated_identical_failure_triggers_reflection():
    m, ctx, events = monitor()
    for _ in range(REPEAT_LIMIT - 1):
        m.observe("browser_click", {"role": "button", "name": "Go"}, FAIL)
        assert m.end_turn() is None
    m.observe("browser_click", {"role": "button", "name": "Go"}, FAIL)
    message = m.end_turn()
    assert "stuck" in message and "failed 3 times" in message
    assert ctx.state.counters["recovery:reflection"] == 1


def test_a_different_call_resets_the_repeat_count():
    m, _, _ = monitor()
    for name in ("A", "A", "B", "A", "A"):
        m.observe("browser_click", {"role": "button", "name": name}, FAIL)
        assert m.end_turn() is None


def test_stall_without_progress_then_progress_resets():
    m, ctx, _ = monitor()
    for _ in range(STALL_TURNS - 1):
        m.observe("browser_snapshot", {}, OK)
        assert m.end_turn() is None
    ctx.state.outcomes.append(Outcome(item="x", status="done"))  # progress just in time
    assert m.end_turn() is None
    for _ in range(STALL_TURNS - 1):
        assert m.end_turn() is None
    assert "no progress" in m.end_turn()


def test_escalates_to_a_human_after_two_reflections():
    m, ctx, events = monitor(answers=["Skip that record and continue"])
    replies = []
    for _ in range(3):
        for _ in range(REPEAT_LIMIT):
            m.observe("read_pdf", {"path": "x.pdf"}, FAIL)
        replies.append(m.end_turn())
    assert "Re-read the latest page" in replies[0] and "Re-read the latest page" in replies[1]
    assert "Skip that record and continue" in replies[2]
    assert len(ctx.human.asked) == 1 and len(recoveries(events, "escalated")) == 1


def test_recognises_relocated_control_and_reauthentication_and_correction():
    m, ctx, events = monitor()
    m.observe("browser_click", {"role": "button", "name": "Create"}, FAIL)
    m.observe("browser_click", {"role": "button", "name": "Save"}, OK)
    assert recoveries(events, "element_relocated") == [{"type": "element_relocated", "from": "Create", "to": "Save"}]

    m.observe("browser_navigate", {"url": "/x"}, ToolResult.fail("auth", "session expired"))
    ctx._browser = SimpleNamespace(logged_in=True)
    m.observe("browser_click", {"role": "button", "name": "Sign in"}, OK)
    assert len(recoveries(events, "reauthenticated")) == 1

    submit = {"role": "button", "name": "Save"}
    m.observe("browser_click", submit, ToolResult.fail("validation", "rejected"))
    m.observe("browser_fill", {"label": "Ref", "value": "R-2"}, OK)
    m.observe("browser_click", submit, OK)
    assert len(recoveries(events, "input_corrected")) == 1


def test_note_recovery_counts_by_type():
    _, ctx, events = monitor()
    note_recovery(ctx, "transient_retry", attempt=1)
    note_recovery(ctx, "transient_retry", attempt=2)
    assert ctx.state.counters["recovery:transient_retry"] == 2 and len(events) == 2
