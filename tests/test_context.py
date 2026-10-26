"""Context trimming drops stale page snapshots but never facts, unseen results, or tool-call pairing."""
import json

from agent.context import OMITTED, trim


def observation(i: int) -> dict:
    data = {"url": f"http://x/{i}", "status": 200, "notices": [f"record {i} saved"], "alerts": [], "page": "x" * 3000}
    return {"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps({"ok": True, "data": data})}


def pages(messages) -> list[bool]:
    return [json.loads(m["content"])["data"]["page"] == OMITTED for m in messages if m["role"] == "tool"]


def test_seen_snapshots_are_trimmed_but_facts_and_pairing_survive():
    messages = [{"role": "system", "content": "s"}]
    for i in range(4):
        messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}"}]})
        messages.append(observation(i))
    trimmed = trim(messages, keep_full=2)

    assert len(trimmed) == len(messages)
    assert [m.get("tool_call_id") for m in trimmed] == [m.get("tool_call_id") for m in messages]
    # c3 is unseen (after the last turn); of the seen ones c1, c2 are the 2 most recent
    assert pages(trimmed) == [True, False, False, False]
    assert json.loads(trimmed[2]["content"])["data"]["notices"] == ["record 0 saved"]  # facts survive
    assert not pages(messages)[0]  # the stored history is untouched


def test_unseen_results_of_a_big_batch_are_never_trimmed():
    # Regression: a turn that opened 10 pages must show the model all 10 before any are trimmed.
    batch = [observation(i) for i in range(10)]
    messages = [{"role": "system", "content": "s"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}"} for i in range(10)]}, *batch]
    assert pages(trim(messages, keep_full=2)) == [False] * 10
