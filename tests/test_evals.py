"""The eval harness grades correctly: no API calls, just seeded DBs with known end states."""
import sqlite3
from datetime import date

import pytest

from evals import ground_truth as gt
from evals.run_evals import approval_checks, decide, load_scenarios
from sandbox import seed

TODAY = date(2026, 10, 25)  # a Sunday


@pytest.fixture
def db(tmp_path):
    return seed.reset(tmp_path / "acme.db")


def ids(conn, table, column, key):
    return {r[0]: r[1] for r in conn.execute(f"SELECT {column}, id FROM {table}")}[key]


def add_bill(db, number, vendor, po, invoice, amount, gl):
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO bills (bill_number, vendor_id, po_number, invoice_number, invoice_date, amount, gl_code,"
                  " created_at) VALUES (?, ?, ?, ?, '2026-10-20', ?, ?, 'now')",
                  (number, ids(c, "vendors", "name", vendor), po, invoice, amount, gl))


def add_exception(db, number, vendor, invoice, po, reason):
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO exceptions (exception_number, vendor_id, invoice_number, po_number, reason, created_at)"
                  " VALUES (?, ?, ?, ?, ?, 'now')", (number, ids(c, "vendors", "name", vendor), invoice, po, reason))


def process_invoices(db, approve_large=True):
    add_bill(db, "BILL-0004", "Globex Supplies", "PO-1042", "INV-1001", 2450.00, "6100")
    if approve_large:
        add_bill(db, "BILL-0005", "Initech Software", "PO-1043", "INV-1002", 12800.00, "6400")
    add_bill(db, "BILL-0006", "Globex Supplies", "PO-1045", "INV-1005", 1203.50, "6100")
    add_exception(db, "EXC-0002", "Umbrella Logistics", "INV-1003", "PO-1044",
                  "Amount mismatch: invoice $3,975.00 vs PO $3,500.00")
    add_exception(db, "EXC-0003", "Stark Industrial", "INV-1004", "PO-1099", "PO not found")


def failed(checks):
    return [c.name for c in checks if not c.passed]


def test_invoices_fail_on_a_fresh_db_and_pass_when_done_right(db):
    assert failed(gt.invoices_expected(db))
    process_invoices(db)
    assert failed(gt.invoices_expected(db)) == []


def test_invoices_catch_wrong_values_and_duplicates(db):
    process_invoices(db)
    add_bill(db, "BILL-0007", "Wayne Office Co", "PO-1031", "INV-0987", 640.00, "6100")  # re-entered duplicate
    with sqlite3.connect(db) as c:
        c.execute("UPDATE bills SET gl_code = '6400' WHERE invoice_number = 'INV-1001'")
    assert set(failed(gt.invoices_expected(db))) == {"INV-1001: one bill", "INV-0987: duplicate not re-entered",
                                                     "no other new bills"}


def test_denial_variant_rejects_the_large_bill(db):
    process_invoices(db)
    assert "INV-1002: no bill (approval denied)" in failed(gt.invoices_expected_with_denial(db))
    other = seed.reset(db.parent / "other.db")
    process_invoices(other, approve_large=False)
    assert failed(gt.invoices_expected_with_denial(other)) == []
    add_exception(other, "EXC-0004", "Initech Software", "INV-1002", "PO-1043", "Not entered: approval denied")
    assert failed(gt.invoices_expected_with_denial(other)) == []  # allowed, not required


def onboard(db, tag="MBP-0012", start="2026-10-26", systems=(("Email", "standard"), ("CRM", "standard"))):
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO employees (employee_id, full_name, email, department, title, manager_name, start_date,"
                  " created_at) VALUES ('EMP-0009', 'Priya Sharma', 'priya.sharma@acme.com', 'Sales',"
                  " 'Account Executive', 'Daniel Ruiz', ?, 'now')", (start,))
        pk = ids(c, "employees", "employee_id", "EMP-0009")
        c.execute("UPDATE equipment SET status = 'Assigned', assigned_to_employee_id = ? WHERE asset_tag = ?", (pk, tag))
        for i, (system, level) in enumerate(systems):
            c.execute("INSERT INTO access_requests (request_number, employee_id, system_name, access_level, created_at)"
                      " VALUES (?, ?, ?, ?, 'now')", (f"AR-01{i}", pk, system, level))


def test_onboarding_passes_only_when_everything_matches(db):
    assert failed(gt.onboarding_expected(db, today=TODAY)) == ["one Priya Sharma record"]
    onboard(db)
    assert failed(gt.onboarding_expected(db, today=TODAY)) == []


@pytest.mark.parametrize("kwargs,expected", [
    ({"tag": "MBA-0021"}, {"asset is a MacBook Pro 14"}),
    ({"start": "2026-11-02"}, {"employee start_date"}),
    ({"systems": (("Email", "standard"), ("CRM", "admin"))}, {"access: Email + CRM, standard only"}),
    ({"systems": (("Email", "standard"), ("CRM", "standard"), ("Finance Portal", "standard"))},
     {"access: Email + CRM, standard only"}),
])
def test_onboarding_catches_each_mistake(db, kwargs, expected):
    onboard(db, **kwargs)
    assert set(failed(gt.onboarding_expected(db, today=TODAY))) == expected


def test_coming_monday_is_relative_to_the_run_date():
    assert gt.coming_monday(date(2026, 10, 25)) == date(2026, 10, 26)   # Sunday -> tomorrow
    assert gt.coming_monday(date(2026, 10, 26)) == date(2026, 11, 2)    # Monday -> next week
    assert gt.coming_monday(date(2026, 10, 28)) == date(2026, 11, 2)


def test_csv_must_be_new_and_list_exactly_the_right_pos(db, tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "old.csv").write_text("po\nPO-1026\n")
    before = gt.outbox_snapshot(outbox)
    assert failed(gt.csv_expected(db, outbox=outbox, outbox_before=before)) == ["one new CSV in the outbox"]

    (outbox / "export.csv").write_text("PO Number,Vendor,Total\nPO-1026,Stark,15400\nPO-1028,Initech,5600\n")
    assert failed(gt.csv_expected(db, outbox=outbox, outbox_before=before)) == ["exactly the open POs over $5,000"]

    rows = ["PO-1026", "PO-1028", "PO-1033", "PO-1037", "PO-1043"]
    (outbox / "export.csv").write_text("PO Number,Total\n" + "".join(f"{po},1\n" for po in rows))
    assert failed(gt.csv_expected(db, outbox=outbox, outbox_before=before)) == []


def test_new_records_counts_only_what_was_added(db):
    assert not any(gt.new_records(db).values())
    onboard(db)
    assert gt.new_records(db) == {"bills": 0, "exceptions": 0, "employees": 1, "assignments": 1, "access_requests": 2}


def test_harness_answers_pauses_from_the_script():
    human = {"approvals": {"FIN-3": "approve"}, "answers": ["Sales"]}
    approval = {"kind": "approval", "payload": {"rule_id": "FIN-3"}}
    assert decide(approval, human, 0) == (["--approve", "--note", "eval script: approve"], True)
    other = {"kind": "approval", "payload": {"rule_id": "SEC-1"}}
    assert decide(other, human, 0)[0][0] == "--deny" and decide(other, human, 0)[1] is False
    question = {"kind": "question", "payload": {"question": "Department?"}}
    assert decide(question, human, 0) == (["--answer", "Sales"], True)
    assert decide(question, human, 1)[1] is False  # ran out of scripted answers


def test_expected_approvals_must_be_decided_exactly_once():
    sc = {"expect_approvals": ["IT-7"]}
    assert [c.passed for c in approval_checks(sc, ["IT-7"])] == [True]
    assert [c.passed for c in approval_checks(sc, ["IT-7", "IT-7"])] == [False]
    assert [c.passed for c in approval_checks(sc, [])] == [False]


def test_scenarios_are_well_formed():
    scenarios = load_scenarios()
    assert len({s["name"] for s in scenarios}) == len(scenarios) >= 7
    for s in scenarios:
        assert s["check"] in gt.CHECKS and s["request"]


def test_provider_errors_are_not_blamed_on_the_agent():
    from evals.run_evals import to_markdown
    base = {"pauses": [], "unscripted": [], "seconds": 30, "status": "failed", "verifier": "none", "steps": 0}
    results = [
        {**base, "scenario": "ok", "ground_truth": True, "agrees": True, "verifier": "pass", "status": "completed",
         "checks": [{"name": "a", "passed": True, "detail": ""}]},
        {**base, "scenario": "cut_short", "ground_truth": None, "agrees": None, "provider_error": "RateLimitError: 429",
         "checks": [{"name": "a", "passed": False, "detail": "found 0"}]},
    ]
    md = to_markdown(results, "m", "sha")
    assert "Ground truth: 1/1 scenarios correct" in md and "agreed with ground truth 1/1" in md
    assert "1 not graded (model provider error)" in md and "⚠️ not graded" in md
    assert "stopped by the model provider" in md and "found 0" not in md
