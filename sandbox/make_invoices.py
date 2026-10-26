"""Generates the vendor invoice PDFs that land in the company inbox.

Each invoice is designed to exercise one path through the invoice SOP; `expected`
records the intended outcome and is used only by the eval harness, never the agent.
"""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "company" / "inbox"
EXPECTED_OUTCOMES = Path(__file__).resolve().parent / "expected_outcomes.json"

BILL_TO = ["Acme Corp", "Attn: Accounts Payable", "500 Market Street", "San Francisco, CA 94105"]

INVOICES = [
    {
        "file": "invoice_globex_1001.pdf",
        "vendor": "Globex Supplies",
        "address": ["1200 Industrial Way", "Springfield, IL 62701"],
        "invoice_number": "INV-1001",
        "invoice_date": "2026-10-19",
        "po_number": "PO-1042",
        "items": [("Copy paper, A4 (case of 10 reams)", 40, 32.50), ("Toner cartridge, black", 10, 115.00)],
        "expected": {"outcome": "bill_created", "note": "Matches PO-1042 exactly"},
    },
    {
        "file": "invoice_initech_1002.pdf",
        "vendor": "Initech Software",
        "address": ["4120 Freidrich Lane", "Austin, TX 78744"],
        "invoice_number": "INV-1002",
        "invoice_date": "2026-10-20",
        "po_number": "PO-1043",
        "items": [("TPS Suite annual license (per seat)", 50, 256.00)],
        "expected": {"outcome": "bill_created_after_approval", "note": "Over $10,000, so FIN-3 approval needed"},
    },
    {
        "file": "invoice_umbrella_1003.pdf",
        "vendor": "Umbrella Logistics",
        "address": ["88 Raccoon Street", "Chicago, IL 60607"],
        "invoice_number": "INV-1003",
        "invoice_date": "2026-10-20",
        "po_number": "PO-1044",
        "items": [("Freight - Chicago warehouse transfer", 1, 3500.00), ("Fuel surcharge", 1, 475.00)],
        "expected": {"outcome": "exception_logged", "note": "Amount mismatch: invoice $3,975.00 vs PO $3,500.00"},
    },
    {
        "file": "invoice_stark_1004.pdf",
        "vendor": "Stark Industrial",
        "address": ["10880 Malibu Point", "Los Angeles, CA 90265"],
        "invoice_number": "INV-1004",
        "invoice_date": "2026-10-21",
        "po_number": "PO-1099",
        "items": [("Hydraulic pallet jack", 2, 2650.00)],
        "expected": {"outcome": "exception_logged", "note": "PO not found (PO-1099 does not exist)"},
    },
    {
        "file": "invoice_wayne_0987.pdf",
        "vendor": "Wayne Office Co",
        "address": ["1007 Mountain Drive", "Gotham, NJ 07001"],
        "invoice_number": "INV-0987",
        "invoice_date": "2026-08-18",
        "po_number": "PO-1031",
        "items": [("Desk organizer set", 20, 18.00), ("Stationery kit", 14, 20.00)],
        "expected": {"outcome": "skipped_duplicate", "note": "Already entered as BILL-0001"},
    },
    {
        "file": "invoice_globex_1005.pdf",
        "vendor": "Globex Supplies",
        "address": ["1200 Industrial Way", "Springfield, IL 62701"],
        "invoice_number": "INV-1005",
        "invoice_date": "2026-10-22",
        "po_number": "PO-1045",
        "items": [("Ergonomic office chair", 4, 300.00), ("Delivery fee", 1, 3.50)],
        "expected": {"outcome": "bill_created", "note": "$3.50 over PO-1045, within the $5.00 tolerance"},
    },
]


def invoice_total(inv: dict) -> float:
    return round(sum(qty * price for _, qty, price in inv["items"]), 2)


def _draw_invoice(path: Path, inv: dict) -> None:
    # invariant=1 drops timestamps so regenerated PDFs are byte-identical (clean git diffs).
    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    width, height = LETTER
    left, y = 54, height - 60

    c.setFont("Helvetica-Bold", 16)
    c.drawString(left, y, inv["vendor"])
    c.setFont("Helvetica", 10)
    for line in inv["address"]:
        y -= 14
        c.drawString(left, y, line)

    c.setFont("Helvetica-Bold", 26)
    c.drawRightString(width - 54, height - 64, "INVOICE")

    y -= 36
    c.setFont("Helvetica", 11)
    for text in (
        f"Invoice Number: {inv['invoice_number']}",
        f"Invoice Date: {inv['invoice_date']}",
        f"PO Number: {inv['po_number']}",
        "Payment Terms: Net 30",
    ):
        c.drawString(left, y, text)
        y -= 16

    y -= 10
    c.setFont("Helvetica-Bold", 11)
    c.drawString(left, y, "Bill To:")
    c.setFont("Helvetica", 10)
    for line in BILL_TO:
        y -= 14
        c.drawString(left + 10, y, line)

    y -= 34
    cols = (left, 340, 410, 490)
    c.setFont("Helvetica-Bold", 10)
    for x, head in zip(cols, ("Description", "Qty", "Unit Price", "Amount")):
        c.drawString(x, y, head)
    c.line(left, y - 4, width - 54, y - 4)
    c.setFont("Helvetica", 10)
    for desc, qty, price in inv["items"]:
        y -= 18
        c.drawString(cols[0], y, desc)
        c.drawString(cols[1], y, str(qty))
        c.drawString(cols[2], y, f"${price:,.2f}")
        c.drawString(cols[3], y, f"${qty * price:,.2f}")

    total = invoice_total(inv)
    y -= 30
    c.line(380, y + 14, width - 54, y + 14)
    for text, bold in ((f"Subtotal: ${total:,.2f}", False), ("Tax: $0.00", False), (f"Total: ${total:,.2f}", True)):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11)
        c.drawRightString(width - 54, y, text)
        y -= 16

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(left, 60, f"Please reference {inv['invoice_number']} and {inv['po_number']} with your payment.")
    c.save()


def generate(out_dir: Path = INBOX) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.pdf"):
        old.unlink()
    paths = []
    for inv in INVOICES:
        path = out_dir / inv["file"]
        _draw_invoice(path, inv)
        paths.append(path)
    return paths


def write_expected_outcomes(path: Path = EXPECTED_OUTCOMES) -> None:
    rows = [
        {
            "file": inv["file"],
            "vendor": inv["vendor"],
            "invoice_number": inv["invoice_number"],
            "po_number": inv["po_number"],
            "total": invoice_total(inv),
            **inv["expected"],
        }
        for inv in INVOICES
    ]
    path.write_text(json.dumps(rows, indent=2) + "\n")


if __name__ == "__main__":
    for p in generate():
        print(f"wrote {p.relative_to(ROOT)}")
    write_expected_outcomes()
