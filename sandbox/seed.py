"""Resets the sandbox to a known state: fresh DB, seed data, invoice PDFs.

Run with `python -m sandbox.seed` (or `make reset`). Deterministic and re-runnable.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from sandbox import db, make_invoices

VENDORS = [
    ("Globex Supplies", "Office Supplies", "6100"),
    ("Initech Software", "Software", "6400"),
    ("Umbrella Logistics", "Shipping", "6200"),
    ("Stark Industrial", "Equipment", "1500"),
    ("Wayne Office Co", "Office Supplies", "6100"),
]

# (po_number, vendor, description, total, status, created_date)
PURCHASE_ORDERS = [
    # The five POs the inbox invoices point at.
    ("PO-1042", "Globex Supplies", "Copy paper and toner - Q4 restock", 2450.00, "Open", "2026-09-28"),
    ("PO-1043", "Initech Software", "TPS Suite annual license (50 seats)", 12800.00, "Open", "2026-09-30"),
    ("PO-1044", "Umbrella Logistics", "Freight - Chicago warehouse transfer", 3500.00, "Open", "2026-10-01"),
    ("PO-1045", "Globex Supplies", "Ergonomic office chairs (4)", 1200.00, "Open", "2026-10-05"),
    ("PO-1031", "Wayne Office Co", "Desk organizers and stationery", 640.00, "Closed", "2026-08-14"),
    # Distractors: same vendors, similar amounts and descriptions, so matching needs the PO number.
    ("PO-1020", "Stark Industrial", "Forklift maintenance contract", 8750.00, "Closed", "2026-06-02"),
    ("PO-1021", "Globex Supplies", "Breakroom supplies", 385.40, "Closed", "2026-06-10"),
    ("PO-1022", "Initech Software", "Cloud backup add-on", 2200.00, "Closed", "2026-06-18"),
    ("PO-1023", "Umbrella Logistics", "Pallet shipping - Dallas", 1875.00, "Closed", "2026-06-25"),
    ("PO-1024", "Wayne Office Co", "Whiteboards (6)", 720.00, "Closed", "2026-07-01"),
    ("PO-1025", "Umbrella Logistics", "Expedited freight - Q3 launch", 6400.00, "Closed", "2026-07-09"),
    ("PO-1026", "Stark Industrial", "Warehouse shelving units", 15400.00, "Open", "2026-07-15"),
    ("PO-1027", "Globex Supplies", "Copy paper and toner - Q3 restock", 2450.00, "Closed", "2026-07-20"),
    ("PO-1028", "Initech Software", "Security audit tooling", 5600.00, "Open", "2026-07-28"),
    ("PO-1029", "Wayne Office Co", "Conference room chairs", 2980.00, "Open", "2026-08-03"),
    ("PO-1030", "Globex Supplies", "Toner cartridges", 910.00, "Open", "2026-08-09"),
    ("PO-1032", "Stark Industrial", "Safety equipment", 4320.00, "Open", "2026-08-19"),
    ("PO-1033", "Umbrella Logistics", "International freight - Rotterdam", 9850.00, "Open", "2026-08-26"),
    ("PO-1034", "Initech Software", "Developer tooling bundle", 7300.00, "Closed", "2026-09-02"),
    ("PO-1035", "Globex Supplies", "Office supplies - September", 1640.00, "Open", "2026-09-08"),
    ("PO-1036", "Wayne Office Co", "Filing cabinets", 1150.00, "Open", "2026-09-12"),
    ("PO-1037", "Stark Industrial", "Loading dock repair", 11200.00, "Open", "2026-09-16"),
    ("PO-1038", "Umbrella Logistics", "Courier services - October", 780.00, "Open", "2026-09-20"),
    ("PO-1039", "Initech Software", "Helpdesk seats (10)", 3100.00, "Open", "2026-09-23"),
    ("PO-1040", "Globex Supplies", "Standing desk mats (8)", 1200.00, "Open", "2026-09-25"),
]

# (bill_number, vendor, po_number, invoice_number, invoice_date, amount)
BILLS = [
    ("BILL-0001", "Wayne Office Co", "PO-1031", "INV-0987", "2026-08-18", 640.00),
    ("BILL-0002", "Globex Supplies", "PO-1021", "INV-0951", "2026-06-15", 385.40),
    ("BILL-0003", "Umbrella Logistics", "PO-1025", "INV-0963", "2026-07-14", 6400.00),
]

# (exception_number, vendor, invoice_number, po_number, reason)
EXCEPTIONS = [
    ("EXC-0001", "Stark Industrial", "INV-0940", "PO-1020", "Amount mismatch: invoice $8,900.00 vs PO $8,750.00"),
]

# (employee_id, full_name, department, title, manager_name, start_date)
EMPLOYEES = [
    ("EMP-0001", "Daniel Ruiz", "Sales", "Sales Manager", "Sara Lindqvist", "2023-02-06"),
    ("EMP-0002", "Maria Chen", "Engineering", "Engineering Manager", "Sara Lindqvist", "2022-09-12"),
    ("EMP-0003", "James Okafor", "Finance", "Finance Controller", "Sara Lindqvist", "2021-05-03"),
    ("EMP-0004", "Sara Lindqvist", "HR", "HR Manager", "", "2020-01-13"),
    ("EMP-0005", "Tom Becker", "Sales", "Account Executive", "Daniel Ruiz", "2025-03-17"),
    ("EMP-0006", "Aisha Khan", "Engineering", "Software Engineer", "Maria Chen", "2024-07-01"),
    ("EMP-0007", "Lucas Moreau", "Operations", "IT Administrator", "Sara Lindqvist", "2023-11-20"),
    ("EMP-0008", "Emily Park", "Marketing", "Marketing Lead", "Sara Lindqvist", "2024-01-08"),
]

# (asset_tag, model, value, status, assigned_to employee_id)
EQUIPMENT = [
    ("MBP-0008", "MacBook Pro 14", 2499.00, "Assigned", "EMP-0005"),
    ("MBP-0011", "MacBook Pro 14", 2499.00, "In Repair", None),
    ("MBP-0012", "MacBook Pro 14", 2499.00, "Available", None),
    ("MBP-0013", "MacBook Pro 14", 2499.00, "Available", None),
    ("MBA-0019", "MacBook Air", 1199.00, "Assigned", "EMP-0008"),
    ("MBA-0021", "MacBook Air", 1199.00, "Available", None),
    ("MON-0101", "Dell 27\" Monitor", 329.00, "Available", None),
]

# (request_number, employee_id, system, level)
ACCESS_REQUESTS = [
    ("AR-0001", "EMP-0005", "Email", "standard"),
    ("AR-0002", "EMP-0005", "CRM", "standard"),
]


def email_for(full_name: str) -> str:
    first, last = full_name.lower().split(" ", 1)
    return f"{first}.{last.replace(' ', '')}@acme.com"


def reset(path: Path | None = None) -> Path:
    path = path or db.db_path()
    if path.exists():
        path.unlink()
    conn = db.connect(path)
    conn.executescript(db.SCHEMA)
    ts = "2026-10-01T09:00:00"

    conn.execute(
        "INSERT INTO users (username, password) VALUES (?, ?)",
        (os.environ.get("ACME_ERP_USER", "agent"), os.environ.get("ACME_ERP_PASSWORD", "acme-demo-123")),
    )
    conn.executemany("INSERT INTO vendors (name, category, gl_code) VALUES (?, ?, ?)", VENDORS)
    vendor_id = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM vendors")}

    conn.executemany(
        "INSERT INTO purchase_orders (po_number, vendor_id, description, total_amount, status, created_date)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(po, vendor_id[v], desc, amt, status, d) for po, v, desc, amt, status, d in PURCHASE_ORDERS],
    )
    gl_code = {name: gl for name, _, gl in VENDORS}
    conn.executemany(
        "INSERT INTO bills (bill_number, vendor_id, po_number, invoice_number, invoice_date, amount, gl_code, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(b, vendor_id[v], po, inv, d, amt, gl_code[v], ts) for b, v, po, inv, d, amt in BILLS],
    )
    conn.executemany(
        "INSERT INTO exceptions (exception_number, vendor_id, invoice_number, po_number, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(e, vendor_id[v], inv, po, reason, ts) for e, v, inv, po, reason in EXCEPTIONS],
    )
    conn.executemany(
        "INSERT INTO employees (employee_id, full_name, email, department, title, manager_name, start_date, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(eid, name, email_for(name), dept, title, mgr, start, ts) for eid, name, dept, title, mgr, start in EMPLOYEES],
    )
    emp_pk = {row["employee_id"]: row["id"] for row in conn.execute("SELECT id, employee_id FROM employees")}
    conn.executemany(
        "INSERT INTO equipment (asset_tag, model, asset_value, status, assigned_to_employee_id) VALUES (?, ?, ?, ?, ?)",
        [(tag, model, val, status, emp_pk.get(emp)) for tag, model, val, status, emp in EQUIPMENT],
    )
    conn.executemany(
        "INSERT INTO access_requests (request_number, employee_id, system_name, access_level, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(ar, emp_pk[emp], system, level, ts) for ar, emp, system, level in ACCESS_REQUESTS],
    )
    conn.commit()
    conn.close()
    return path


def main() -> None:
    # Same .env the agent reads, so the seeded login always matches the agent's credentials.
    load_dotenv(make_invoices.ROOT / ".env")
    path = reset()
    pdfs = make_invoices.generate()
    make_invoices.write_expected_outcomes()
    print(f"Sandbox reset: {path}")
    print(f"  {len(PURCHASE_ORDERS)} purchase orders, {len(BILLS)} bills, {len(EMPLOYEES)} employees, {len(EQUIPMENT)} assets")
    print(f"  {len(pdfs)} invoice PDFs in {make_invoices.INBOX.relative_to(make_invoices.ROOT)}/")


if __name__ == "__main__":
    main()
