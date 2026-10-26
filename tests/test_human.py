"""Durable human-in-the-loop: the file channel pauses the run and answers from stored responses."""
import json

import pytest

from agent.human import FileChannel, HumanResponse, RunPaused, request_key
from agent.models import RunState

APPROVAL = {"summary": "create record", "rule_id": "FIN-3", "params": {"amount": "12800.00", "number": "N-1"}}


def test_pauses_and_writes_the_request(tmp_path):
    channel = FileChannel(RunState(run_id="t"), tmp_path)
    with pytest.raises(RunPaused) as paused:
        channel.ask("approval", APPROVAL)
    pending = json.loads((tmp_path / "pending.json").read_text())
    assert pending["kind"] == "approval" and pending["payload"]["rule_id"] == "FIN-3"
    assert pending["key"] == paused.value.request["key"]


def store(state, kind, payload, response):
    state.human_responses[request_key(kind, payload)] = {
        "kind": kind, "rule_id": payload.get("rule_id"), "response": response.model_dump(), "consumed": False}


def test_answers_from_stored_response_without_pausing(tmp_path):
    state = RunState(run_id="t")
    store(state, "approval", APPROVAL, HumanResponse(approved=True, note="ok"))
    retyped = {**APPROVAL, "params": {"amount": "$12,800", "number": "n-1 "}}  # same values, typed differently
    assert FileChannel(state, tmp_path).ask("approval", retyped).approved is True
    assert not (tmp_path / "pending.json").exists()


def test_rephrased_question_gets_the_stored_answer(tmp_path):
    state = RunState(run_id="t")
    store(state, "question", {"question": "Which team are they joining?"}, HumanResponse(answer="Sales"))
    assert FileChannel(state, tmp_path).ask("question", {"question": "What department?"}).answer == "Sales"
    with pytest.raises(RunPaused):  # ...but only once: a genuinely new question pauses again
        FileChannel(state, tmp_path).ask("question", {"question": "Who is their manager?"})


def test_request_key_ignores_formatting_but_not_values():
    other = {**APPROVAL, "params": {"amount": "12900", "number": "N-1"}}
    assert request_key("approval", APPROVAL) == request_key("approval", {**APPROVAL, "params": {"amount": "12,800", "number": "N-1"}})
    assert request_key("approval", APPROVAL) != request_key("approval", other)


def test_an_approval_never_covers_a_different_action(tmp_path):
    # Seen in an eval: after resuming, a submission with an empty field came first and must not
    # inherit the approval the human gave for the complete one.
    state = RunState(run_id="t")
    store(state, "approval", APPROVAL, HumanResponse(approved=True))
    different = {**APPROVAL, "params": {"amount": "12800.00", "number": "— select —"}}
    with pytest.raises(RunPaused):
        FileChannel(state, tmp_path).ask("approval", different)
    assert FileChannel(state, tmp_path).ask("approval", APPROVAL).approved is True  # the real one still is
