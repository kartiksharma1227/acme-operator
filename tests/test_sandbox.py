"""Sandbox ERP behaviour the operator (and the demo) depends on."""
import re

import pytest
from fastapi.testclient import TestClient

from sandbox import app as erp
from sandbox import seed
from sandbox.chaos import ChaosConfig

PASSWORD = "acme-demo-123"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "acme.db"
    monkeypatch.setenv("ACME_DB", str(db_file))
    monkeypatch.setenv("ACME_ERP_PASSWORD", PASSWORD)
    seed.reset(db_file)
    erp.app.state.chaos = ChaosConfig()
    yield TestClient(erp.app)
    erp.app.state.chaos = ChaosConfig()


def login(client: TestClient) -> None:
    r = client.post("/login", data={"username": "agent", "password": PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303


def vendor_id(client: TestClient, name: str) -> str:
    html = client.get("/bills/new").text
    return re.search(rf'<option value="(\d+)"[^>]*>{name}</option>', html).group(1)


def test_login_required(client):
    r = client.get("/bills", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=/bills")
    assert "Please sign in" in client.get(r.headers["location"]).text


def test_wrong_password(client):
    r = client.post("/login", data={"username": "agent", "password": "nope"})
    assert "Invalid credentials" in r.text


def test_create_bill(client):
    login(client)
    r = client.post("/bills/new", data={
        "vendor_id": vendor_id(client, "Globex Supplies"), "po_number": "PO-1042", "invoice_number": "INV-1001",
        "invoice_date": "2026-10-19", "amount": "$2,450.00", "gl_code": "6100",
    })
    assert r.status_code == 200
    assert "Bill BILL-0004 created" in r.text
    assert "$2,450.00" in r.text


def test_unknown_po_rejected(client):
    login(client)
    r = client.post("/bills/new", data={
        "vendor_id": vendor_id(client, "Stark Industrial"), "po_number": "PO-1099", "invoice_number": "INV-1004",
        "invoice_date": "2026-10-21", "amount": "5300", "gl_code": "1500",
    })
    assert r.status_code == 400
    assert "PO not found" in r.text
    assert 'value="INV-1004"' in r.text  # form keeps what was typed


def test_duplicates_are_not_blocked_by_the_erp(client):
    # The legacy ERP accepts a second bill for an existing invoice; catching it is the operator's job.
    login(client)
    r = client.post("/bills/new", data={
        "vendor_id": vendor_id(client, "Wayne Office Co"), "po_number": "PO-1031", "invoice_number": "INV-0987",
        "invoice_date": "2026-08-18", "amount": "640", "gl_code": "6100",
    })
    assert "created" in r.text


def test_po_pagination(client):
    login(client)
    po_links = lambda html: re.findall(r'href="/purchase-orders/PO-\d+"', html)
    assert len(po_links(client.get("/purchase-orders").text)) == 10
    page3 = client.get("/purchase-orders?page=3").text
    assert len(po_links(page3)) == 5
    assert "Page 3 of 3" in page3
    assert len(po_links(client.get("/purchase-orders?q=Globex").text)) == 7


def test_unknown_po_detail_404(client):
    login(client)
    r = client.get("/purchase-orders/PO-1099")
    assert r.status_code == 404
    assert "Purchase order not found" in r.text


def test_unavailable_asset_cannot_be_assigned(client):
    login(client)
    r = client.post("/equipment/MBP-0011", data={"employee_id": "1"})
    assert r.status_code == 409
    assert "not available (status: In Repair)" in r.text


def test_assign_available_asset(client):
    login(client)
    r = client.post("/equipment/MBP-0012", data={"employee_id": "1"})
    assert "MBP-0012 assigned to Daniel Ruiz" in r.text


def test_chaos_rename(client):
    login(client)
    assert "Create Bill" in client.get("/bills/new").text
    erp.app.state.chaos = ChaosConfig(modes={"rename"})
    html = client.get("/bills/new").text
    assert "Save Bill" in html and "Create Bill" not in html


def test_chaos_expiry(client):
    erp.app.state.chaos = ChaosConfig(modes={"expiry"}, expiry_n=3)
    login(client)
    for _ in range(3):
        assert client.get("/vendors", follow_redirects=False).status_code == 200
    r = client.get("/vendors", follow_redirects=False)
    assert r.status_code == 303 and "msg=expired" in r.headers["location"]


def test_chaos_flaky_only_breaks_gets(client):
    erp.app.state.chaos = ChaosConfig(modes={"flaky"}, flaky_rate=1.0)
    login(client)  # /login is exempt, so recovery is always possible
    assert client.get("/vendors").status_code == 500
    assert client.get("/health").status_code == 200


def test_health_probe_is_exempt_from_chaos(client):
    import time
    erp.app.state.chaos = ChaosConfig(modes={"slow", "flaky"}, flaky_rate=1.0)
    started = time.monotonic()
    assert all(client.get("/health").status_code == 200 for _ in range(3))
    assert time.monotonic() - started < 0.5  # start-up probes must not wait on injected delays
