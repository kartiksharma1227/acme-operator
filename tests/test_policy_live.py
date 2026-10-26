"""The gate on a real page: it reads the values the form is about to submit and pauses for a human."""
import sqlite3

from agent.human import ScriptedChannel
from agent.policy import PolicyGate, parse_number
from sandbox.server import sandbox_server
from tests.test_browser_tools import fill_bill, sign_in


def gated(ctx, decision: str) -> ScriptedChannel:
    human = ScriptedChannel(approvals={"FIN-3": decision})
    ctx.human, ctx.gate = human, PolicyGate(ctx.config)
    return human


def test_high_value_submission_waits_for_a_human(ctx, registry, events, tmp_path, monkeypatch):
    human = gated(ctx, "deny")
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        assert any(e == "policy_check" and d["action"] == "unclassified_write" for e, d in events)

        fill_bill(ctx, registry, "PO-1043", "INV-1002", "12800.00", "6400", "2026-10-20", "Initech Software")
        denied = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert denied.error_type == "policy_denied"
        assert ctx.browser.page.url.endswith("/bills/new")  # nothing was submitted
        (kind, payload), = human.asked
        assert kind == "approval" and payload["rule_id"] == "FIN-3"
        assert parse_number(payload["params"]["amount"]) == 12800  # read from the page, not from the model

        registry.run(ctx, "browser_navigate", {"url": "/bills/new"})
        fill_bill(ctx, registry, "PO-1042", "INV-1001", "2450.00", "6100", "2026-10-19", "Globex Supplies")
        created = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert created.ok and "Bill BILL-0004 created" in created.data["notices"]
        assert len(human.asked) == 1  # the normal one went straight through


def test_identical_submission_is_never_made_twice(ctx, registry, tmp_path, monkeypatch):
    gated(ctx, "approve")
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        fill_bill(ctx, registry, "PO-1042", "INV-1001", "2450.00", "6100", "2026-10-19", "Globex Supplies")
        assert registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"}).ok

        registry.run(ctx, "browser_navigate", {"url": "/bills/new"})
        fill_bill(ctx, registry, "PO-1042", "INV-1001", "2,450", "6100", "2026-10-19", "Globex Supplies")
        again = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert again.error_type == "policy_denied" and "BILL-0004" in again.message
        with sqlite3.connect(tmp_path / "acme.db") as db:  # the system really holds just one
            assert db.execute("SELECT COUNT(*) FROM bills WHERE invoice_number = 'INV-1001'").fetchone()[0] == 1


def test_rejected_submission_can_be_corrected(ctx, registry, tmp_path, monkeypatch):
    gated(ctx, "approve")
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        fill_bill(ctx, registry, "PO-1042", "INV-1001", "2450.00", "", "2026-10-19", "Globex Supplies")
        assert registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"}).error_type == "validation"
        assert ctx.state.action_ledger == []
        assert registry.run(ctx, "browser_fill", {"label": "GL Code", "value": "6100"}).ok
        assert registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"}).ok


def test_saved_then_session_expired_is_reported_as_saved(ctx, registry, tmp_path, monkeypatch):
    # Requests: 1 = landing after sign-in, 2-3 = the gate's two pre-submit lookups,
    # 4 = the form POST (saved), 5 = its confirmation page (expired).
    gated(ctx, "approve")
    with sandbox_server(tmp_path, chaos="expiry", CHAOS_EXPIRY_N="4") as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        fill_bill(ctx, registry, "PO-1042", "INV-1001", "2450.00", "6100", "2026-10-19", "Globex Supplies")
        result = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert result.error_type == "auth"
        assert "WAS accepted" in result.message and "Do NOT submit it again" in result.message
        assert result.data["submission_accepted"].startswith("/bills/BILL-0004")
        assert len(ctx.state.action_ledger) == 1  # so the gate will block a resubmission
        with sqlite3.connect(tmp_path / "acme.db") as db:
            assert db.execute("SELECT COUNT(*) FROM bills WHERE invoice_number = 'INV-1001'").fetchone()[0] == 1


def test_bounced_submission_is_reported_as_not_saved(ctx, registry, tmp_path, monkeypatch):
    # Request 1 = landing after sign-in, 2-3 = pre-submit lookups; the POST (4) is bounced to sign-in.
    gated(ctx, "approve")
    with sandbox_server(tmp_path, chaos="expiry", CHAOS_EXPIRY_N="3") as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        fill_bill(ctx, registry, "PO-1042", "INV-1001", "2450.00", "6100", "2026-10-19", "Globex Supplies")
        result = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert result.error_type == "auth" and "NOT submitted" in result.message
        assert "submission_accepted" not in result.data and ctx.state.action_ledger == []


def test_renamed_button_is_still_gated(ctx, registry, tmp_path, monkeypatch):
    human = gated(ctx, "approve")
    with sandbox_server(tmp_path, chaos="rename") as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        fill_bill(ctx, registry, "PO-1043", "INV-1002", "12800.00", "6400", "2026-10-20", "Initech Software")
        result = registry.run(ctx, "browser_click", {"role": "button", "name": "Save Bill"})
        assert result.ok and "Bill BILL-0004 created" in result.data["notices"]
        assert [p["rule_id"] for _, p in human.asked] == ["FIN-3"]


def test_signed_out_during_a_pre_submit_check_submits_nothing(ctx, registry, tmp_path, monkeypatch):
    # Request 1 = landing, 2 = first lookup; the second lookup (3) finds the session expired.
    gated(ctx, "approve")
    with sandbox_server(tmp_path, chaos="expiry", CHAOS_EXPIRY_N="2") as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        fill_bill(ctx, registry, "PO-1042", "INV-1001", "2450.00", "6100", "2026-10-19", "Globex Supplies")
        result = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert result.error_type == "auth" and "nothing was submitted" in result.message  # fails closed
        assert ctx.browser.page.url.endswith("/bills/new")  # the operator's form was left alone
        with sqlite3.connect(tmp_path / "acme.db") as db:
            assert db.execute("SELECT COUNT(*) FROM bills WHERE invoice_number = 'INV-1001'").fetchone()[0] == 0


def test_pre_submit_checks_stop_an_existing_duplicate_and_a_wrong_amount(ctx, registry, events, tmp_path, monkeypatch):
    gated(ctx, "approve")
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        # Already billed before this run (seed data), so the run's own ledger knows nothing about it.
        fill_bill(ctx, registry, "PO-1031", "INV-0987", "640.00", "6100", "2026-08-18", "Wayne Office Co")
        dup = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert dup.error_type == "policy_denied" and "AP-1" in dup.message and "BILL-0001" in dup.message

        registry.run(ctx, "browser_navigate", {"url": "/bills/new"})
        fill_bill(ctx, registry, "PO-1044", "INV-1003", "3975.00", "6200", "2026-10-21", "Umbrella Logistics")
        wrong = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert wrong.error_type == "policy_denied" and "AP-2" in wrong.message and "475.00" in wrong.message

        # the look-alike: same vendor and amount as PO-1042, but closed
        registry.run(ctx, "browser_navigate", {"url": "/bills/new"})
        fill_bill(ctx, registry, "PO-1027", "INV-1001", "2450.00", "6100", "2026-10-19", "Globex Supplies")
        closed = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert closed.error_type == "policy_denied" and "Status is 'Closed'" in closed.message

        # within tolerance ($3.50 over) goes through, and the decision records what was checked
        registry.run(ctx, "browser_navigate", {"url": "/bills/new"})
        fill_bill(ctx, registry, "PO-1045", "INV-1005", "1203.50", "6100", "2026-10-22", "Globex Supplies")
        assert registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"}).ok
        allowed = [d for e, d in events if e == "policy_check" and d["decision"] == "allow" and d["action"] == "create_bill"]
        assert [c["id"] for c in allowed[-1]["checks"]] == ["AP-1", "AP-2"]
        with sqlite3.connect(tmp_path / "acme.db") as db:
            assert db.execute("SELECT invoice_number FROM bills WHERE created_at > '2026-10-02'").fetchall() == [("INV-1005",)]


def test_placeholder_choice_counts_as_not_filled_in(ctx, registry, events, tmp_path, monkeypatch):
    human = gated(ctx, "approve")
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        registry.run(ctx, "browser_navigate", {"url": "/equipment/MBP-0012"})  # employee left at "— Select … —"
        result = registry.run(ctx, "browser_click", {"role": "button", "name": "Assign Equipment"})
        assert result.error_type == "validation" and "employee" in result.message
        assert human.asked == []  # nobody is asked to approve an incomplete action
