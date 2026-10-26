"""Browser tools against the real sandbox server, including injected failures.

This is the Phase 3 acceptance: the tools alone can sign in and create a record, and
every failure mode comes back as a typed ToolResult instead of an exception.
"""
import json
from pathlib import Path

from sandbox.server import sandbox_server

REAL_PASSWORD = "acme-demo-123"


def sign_in(ctx, registry, start: str = "/bills/new"):
    first = registry.run(ctx, "browser_navigate", {"url": start})
    assert first.ok and "requires signing in" in first.message
    creds = registry.run(ctx, "get_credentials", {"system": "acme_erp"}).data
    assert registry.run(ctx, "browser_fill", {"label": "Username", "value": creds["username"]}).ok
    filled = registry.run(ctx, "browser_fill", {"label": "Password", "value": creds["password"]})
    assert filled.ok and filled.data["value"] == "***"
    result = registry.run(ctx, "browser_click", {"role": "button", "name": "Sign in"})
    assert result.ok and ctx.browser.logged_in
    return result


def fill_bill(ctx, registry, po: str, invoice: str, amount: str, gl: str, date: str, supplier: str):
    assert registry.run(ctx, "browser_select", {"label": "Vendor", "option": supplier}).ok
    for label, value in (("PO Number", po), ("Invoice Number", invoice), ("Invoice Date", date),
                         ("Amount", amount), ("GL Code", gl)):
        assert registry.run(ctx, "browser_fill", {"label": label, "value": value}).ok, label


def test_sign_in_and_create_record_with_tools_only(ctx, registry, events, tmp_path, monkeypatch):
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        landed = sign_in(ctx, registry)
        assert landed.data["url"].endswith("/bills/new")  # returned to where we were going

        fill_bill(ctx, registry, "PO-1042", "INV-1001", "2450.00", "6100", "2026-10-19", "Globex")
        created = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert created.ok, created.message
        assert "Bill BILL-0004 created" in created.data["notices"]
        assert "/bills/BILL-0004" in created.data["url"]
        assert (ctx.run_dir / created.evidence[0]).exists()

    log = json.dumps(events)
    assert REAL_PASSWORD not in log and REAL_PASSWORD not in created.to_llm()


def test_missing_page_is_information_not_failure(ctx, registry, tmp_path, monkeypatch):
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        result = registry.run(ctx, "browser_navigate", {"url": "/purchase-orders/PO-1099"})
        assert result.ok and result.data["status"] == 404
        assert any("No purchase order" in a for a in result.data["alerts"])


def test_rejected_form_is_validation(ctx, registry, tmp_path, monkeypatch):
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        fill_bill(ctx, registry, "PO-1099", "INV-1004", "5300", "1500", "2026-10-21", "Stark Industrial")
        result = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert not result.ok and result.error_type == "validation"
        assert "PO not found" in result.message and "nothing was saved" in result.message


def test_renamed_button_lists_alternatives(ctx, registry, tmp_path, monkeypatch):
    with sandbox_server(tmp_path, chaos="rename") as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)
        result = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert result.error_type == "not_found"
        assert "Save Bill" in result.data["buttons"]


def test_session_expiry_is_auth(ctx, registry, events, tmp_path, monkeypatch):
    with sandbox_server(tmp_path, chaos="expiry", CHAOS_EXPIRY_N="3") as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry, start="/vendors")  # authenticated request 1
        results = [registry.run(ctx, "browser_navigate", {"url": "/vendors"}) for _ in range(3)]
        assert [r.ok for r in results] == [True, True, False]
        assert results[-1].error_type == "auth" and not ctx.browser.logged_in
        assert any(e == "recovery" and d["type"] == "session_expired" for e, d in events)
        sign_in(ctx, registry, start="/vendors")  # and recovery works


def test_server_errors_are_retried_then_transient(ctx, registry, events, tmp_path, monkeypatch):
    with sandbox_server(tmp_path, chaos="flaky", CHAOS_FLAKY_RATE="1.0") as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        # Sign-in (and the demo-only chaos page we return to) are never flaky, so recovery stays possible.
        sign_in(ctx, registry, start="/login?next=/admin/chaos")
        result = registry.run(ctx, "browser_navigate", {"url": "/vendors"})
        assert not result.ok and result.error_type == "transient"
        assert sum(e == "recovery" for e, _ in events) == 3
        assert ctx.state.counters["recovery:transient_retry"] == 3


def test_unreachable_system_is_transient(ctx, registry, monkeypatch):
    monkeypatch.setenv("ACME_ERP_URL", "http://127.0.0.1:9")  # nothing listens here
    result = registry.run(ctx, "browser_navigate", {"url": "/"})
    assert not result.ok and result.error_type == "transient"


def test_same_target_links_are_not_ambiguous(ctx, registry, tmp_path, monkeypatch):
    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)  # on the new-record form: menu link + breadcrumb share a name
        result = registry.run(ctx, "browser_click", {"role": "link", "name": "Bills"})
        assert result.ok and result.data["url"].endswith("/bills")


def test_gate_hook_runs_before_submit(ctx, registry, tmp_path, monkeypatch):
    calls = []

    def gate(run_ctx, page, target):
        calls.append(Path(page.url).name)
        from agent.models import ToolResult
        return ToolResult.fail("policy_denied", "blocked by test gate")

    with sandbox_server(tmp_path) as base:
        monkeypatch.setenv("ACME_ERP_URL", base)
        sign_in(ctx, registry)  # the sign-in submit happens before the gate is installed
        ctx.gate = gate
        registry.run(ctx, "browser_click", {"role": "link", "name": "Bills"})  # not a submit: no gate
        assert calls == []
        registry.run(ctx, "browser_navigate", {"url": "/bills/new"})
        result = registry.run(ctx, "browser_click", {"role": "button", "name": "Create Bill"})
        assert result.error_type == "policy_denied" and calls == ["new"]
