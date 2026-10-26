"""Policy gate decisions in plain code: no browser, no model."""
from types import SimpleNamespace

import pytest

from agent.config import load_config
from agent.human import ScriptedChannel
from agent.models import RunState, ToolResult
from agent.policy import ActionDescriptor, PolicyGate, canonical, grant_key, parse_number, rule_matches

CONFIG = load_config()
RULES = {r["id"]: r["when"] for r in CONFIG.policies["rules"]}


@pytest.mark.parametrize("text,expected", [("$12,800.00", 12800.0), ("1203.5", 1203.5), (" 2,450 ", 2450.0),
                                           ("abc", None), (None, None), (42, 42.0)])
def test_parse_number(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize("amount,fires", [("$12,800.00", True), ("10000.01", True), ("10000", False),
                                          ("2450.00", False), ("", False), (None, False)])
def test_high_value_rule(amount, fires):
    assert rule_matches(RULES["FIN-3"], {"amount": amount}) is fires


def test_equality_rules_are_case_insensitive():
    assert rule_matches(RULES["SEC-1"], {"level": "Admin"})
    assert not rule_matches(RULES["SEC-1"], {"level": "standard"})
    assert rule_matches(RULES["SEC-2"], {"system": "finance portal"})
    assert rule_matches({"field": "x", "op": "in", "value": ["A", "B"]}, {"x": "b"})


def fake_ctx(human):
    events = []
    return SimpleNamespace(state=RunState(run_id="t"), human=human,
                           emit=lambda t, d: events.append((t, d)), events=events)


def bill(amount: str) -> ActionDescriptor:
    return ActionDescriptor("erp", "create_bill", "write", {"vendor": "V", "invoice_number": "N-1", "amount": amount})


def test_approval_is_requested_once_and_remembered():
    human = ScriptedChannel(approvals={"FIN-3": "approve"})
    ctx, gate = fake_ctx(human), PolicyGate(CONFIG)
    assert gate.decide(ctx, bill("$12,800.00")) is None
    assert gate.decide(ctx, bill("$12,800.00")) is None  # same action again: grant reused
    assert len(human.asked) == 1 and human.asked[0][1]["rule_id"] == "FIN-3"
    assert len(ctx.state.grants) == 1


def test_denied_approval_blocks():
    ctx = fake_ctx(ScriptedChannel(approvals={"FIN-3": "deny"}))
    result = PolicyGate(CONFIG).decide(ctx, bill("12800"))
    assert result.error_type == "policy_denied" and "FIN-3" in result.message
    assert not ctx.state.grants


def test_below_threshold_needs_no_human():
    human = ScriptedChannel()
    assert PolicyGate(CONFIG).decide(fake_ctx(human), bill("2450.00")) is None
    assert human.asked == []


def test_deny_rule_never_asks_a_human():
    human = ScriptedChannel(approvals={"*": "approve"})
    request = ActionDescriptor("erp", "request_access", "write", {"employee": "X", "system": "Finance Portal", "level": "standard"})
    result = PolicyGate(CONFIG).decide(fake_ctx(human), request)
    assert result.error_type == "policy_denied" and "SEC-2" in result.message and human.asked == []


def test_grant_is_specific_to_the_values():
    assert grant_key(bill("12800"), "FIN-3") != grant_key(bill("12900"), "FIN-3")
    assert grant_key(bill("12800"), "FIN-3") == grant_key(bill("$12,800.00"), "FIN-3")


@pytest.mark.parametrize("raw,expected", [("$12,800.00", 12800.0), ("1203.5", 1203.5), (" Admin ", "admin"),
                                          ("N-1001", "n-1001"), ("2026-10-26", "2026-10-26"), (None, None)])
def test_canonical(raw, expected):
    assert canonical(raw) == expected


def submit(gate, ctx, descriptor, succeeded: bool):
    blocked = gate.decide(ctx, descriptor)
    if blocked is None:
        result = (ToolResult.success({"url": "http://x/records/R-1?created=1", "notices": ["Record R-1 created"]})
                  if succeeded else ToolResult.fail("validation", "rejected"))
        gate.record(ctx, result)
    return blocked


def test_idempotency_blocks_only_after_success():
    ctx, gate = fake_ctx(ScriptedChannel()), PolicyGate(CONFIG)
    assert submit(gate, ctx, bill("2450"), succeeded=False) is None   # rejected: not recorded
    assert ctx.state.action_ledger == []
    assert submit(gate, ctx, bill("2450"), succeeded=True) is None    # retry succeeds: recorded
    repeat = submit(gate, ctx, bill("$2,450.00"), succeeded=True)     # same record again, typed differently
    assert repeat.error_type == "policy_denied" and "Record R-1 created" in repeat.message
    other = ActionDescriptor("erp", "create_bill", "write", {"vendor": "V", "invoice_number": "N-2", "amount": "2450"})
    assert submit(gate, ctx, other, succeeded=True) is None           # a different record is fine
    assert len(ctx.state.action_ledger) == 2


def test_ledger_survives_a_checkpoint_round_trip():
    ctx, gate = fake_ctx(ScriptedChannel()), PolicyGate(CONFIG)
    submit(gate, ctx, bill("2450"), succeeded=True)
    restored = fake_ctx(ScriptedChannel())
    restored.state = RunState.model_validate_json(ctx.state.model_dump_json())
    assert PolicyGate(CONFIG).decide(restored, bill("2450")).error_type == "policy_denied"


# --- pre-submit checks (agent/checks.py), with a fake lookup instead of a browser ---------------

from agent.checks import Fetched, evaluate, lookup_path  # noqa: E402

CHECKS = {c["id"]: c for c in CONFIG.policies["checks"]}


def lookups(pages: dict[str, Fetched]):
    seen = []

    def fetch(path, labels):
        seen.append(path)
        return pages.get(path, Fetched("not_found"))
    fetch.seen = seen
    return fetch


def test_lookup_paths_are_filled_and_escaped_from_form_values():
    assert lookup_path("/items?q={ref}", {"ref": " A&B 1 "}) == "/items?q=A%26B%201"
    assert lookup_path("/items/{ref}", {"ref": ""}) is None  # can't look up -> the check fails closed


@pytest.mark.parametrize("rows,passed", [
    (["Bill Number Vendor Invoice Number", "BILL-0001 Wayne Office Co INV-0987 PO-1031"], False),
    (["Bill Number Vendor Invoice Number", "BILL-0009 Other Vendor INV-0987"], True),   # same number, other vendor
    (["No bills match \"INV-0987\"."], True),
])
def test_must_not_exist(rows, passed):
    params = {"vendor": "Wayne Office Co", "invoice_number": "INV-0987"}
    result = evaluate(CHECKS["AP-1"], params, lookups({"/bills?q=INV-0987": Fetched("ok", rows=rows)}))
    assert result.passed is passed and not result.unavailable


def po_page(total="$1,200.00", status="Open", vendor="Globex Supplies"):
    return lookups({"/purchase-orders/PO-1": Fetched("ok", shown={"Total": total, "Status": status, "Vendor": vendor})})


def po_bill(amount="1200", vendor="Globex Supplies"):
    return {"po_number": "PO-1", "amount": amount, "vendor": vendor}


@pytest.mark.parametrize("amount,passed", [("1203.50", True), ("$1,205.00", True), ("1205.01", False), ("3975", False)])
def test_must_match_amount_within_tolerance(amount, passed):
    assert evaluate(CHECKS["AP-2"], po_bill(amount), po_page()).passed is passed


def test_must_match_every_shown_fact():
    # The look-alike trap from an eval: same vendor and amount, but the PO is closed.
    closed = evaluate(CHECKS["AP-2"], po_bill(), po_page(status="Closed"))
    assert not closed.passed and "Status is 'Closed'" in closed.detail
    other = evaluate(CHECKS["AP-2"], po_bill(vendor="globex supplies "), po_page(vendor="Initech Software"))
    assert not other.passed and "Vendor is 'Initech Software'" in other.detail
    assert evaluate(CHECKS["AP-2"], po_bill(vendor="globex  supplies"), po_page()).passed  # case/space-insensitive


def test_must_match_fails_when_the_record_is_missing_and_reports_unreachable_lookups():
    missing = evaluate(CHECKS["AP-2"], {**po_bill(), "po_number": "PO-9"}, lookups({}))
    assert not missing.passed and not missing.unavailable and "no record" in missing.detail
    unshown = evaluate(CHECKS["AP-2"], po_bill(), lookups({"/purchase-orders/PO-1": Fetched("ok", shown={})}))
    assert not unshown.passed and "not shown" in unshown.detail
    down = evaluate(CHECKS["AP-2"], po_bill(),
                    lookups({"/purchase-orders/PO-1": Fetched("auth", detail="signed out")}))
    assert not down.passed and down.unavailable == "auth"


def test_a_failing_check_blocks_before_anyone_is_asked_to_approve():
    human = ScriptedChannel(approvals={"FIN-3": "approve"})
    ctx, gate = fake_ctx(human), PolicyGate(CONFIG)
    params = {"vendor": "V", "invoice_number": "N-1", "po_number": "PO-1", "amount": "12800"}
    fetch = lookups({"/bills?q=N-1": Fetched("ok", rows=[]),
                     "/purchase-orders/PO-1": Fetched("ok", shown={"Total": "$9,000.00", "Status": "Open", "Vendor": "V"})})
    result = gate.decide(ctx, ActionDescriptor("erp", "create_bill", "write", params), fetch)
    assert result.error_type == "policy_denied" and "AP-2" in result.message
    assert human.asked == [] and ctx.state.counters["checks_blocked"] == 1


def test_checks_only_run_for_their_action():
    fetch = lookups({})
    ctx = fake_ctx(ScriptedChannel())
    other = ActionDescriptor("erp", "log_exception", "write", {"vendor": "V", "invoice_number": "N-1"})
    assert PolicyGate(CONFIG).decide(ctx, other, fetch) is None and fetch.seen == []


def test_a_half_filled_form_is_refused_before_any_policy_or_human():
    human = ScriptedChannel(approvals={"FIN-3": "approve"})
    ctx = fake_ctx(human)
    result = PolicyGate(CONFIG).decide(ctx, bill(""))
    assert result.error_type == "validation" and "amount" in result.message
    assert human.asked == [] and ctx.events[-1][1]["decision"] == "incomplete"
