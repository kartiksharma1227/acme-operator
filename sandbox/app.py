"""Acme ERP: a small internal web app that the operator uses through a real browser.

Plain server-rendered HTML with proper labels and roles, because the operator reads
pages through the accessibility tree exactly like a screen reader would.
"""
from __future__ import annotations

import secrets
from datetime import date
from math import ceil
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from sandbox import db
from sandbox.chaos import BUTTON_LABELS, ChaosConfig, chaos_middleware, label

HERE = Path(__file__).resolve().parent
PAGE_SIZE = 10
SESSION_COOKIE = "acme_session"
SESSIONS: dict[str, dict] = {}  # token -> {"user": str, "requests": int}
LOGIN_MESSAGES = {
    "signin": "Please sign in",
    "expired": "Your session has expired. Please sign in again.",
}
DEPARTMENTS = ["Sales", "Engineering", "Finance", "HR", "Marketing", "Operations"]
SYSTEMS = ["Email", "CRM", "Finance Portal"]
ACCESS_LEVELS = ["standard", "admin"]

app = FastAPI(title="Acme ERP", docs_url=None, redoc_url=None, openapi_url=None)
app.state.chaos = ChaosConfig.from_env()
app.middleware("http")(chaos_middleware)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["label"] = lambda key: label(app.state.chaos, key)
templates.env.filters["money"] = db.fmt_money


# ---------- auth ----------

class LoginRequired(Exception):
    def __init__(self, next_url: str, reason: str = "signin"):
        self.next_url, self.reason = next_url, reason


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, exc: LoginRequired):
    return RedirectResponse(f"/login?next={quote(exc.next_url)}&msg={exc.reason}", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if exc.detail not in (None, "Not Found") else "Page not found."
    title = "Not found" if exc.status_code == 404 else f"Error {exc.status_code}"
    return render(request, "error.html", status_code=exc.status_code, title=title, message=detail)


def require_user(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    session = SESSIONS.get(token)
    next_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if session is None:
        raise LoginRequired(next_url)
    session["requests"] += 1
    chaos: ChaosConfig = request.app.state.chaos
    if chaos.enabled("expiry") and session["requests"] > chaos.expiry_n:
        SESSIONS.pop(token, None)
        raise LoginRequired(next_url, "expired")
    return session["user"]


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def safe_next(url: str) -> str:
    # Only same-site relative paths, so `next` can't bounce the browser elsewhere.
    return url if url.startswith("/") and not url.startswith("//") else "/"


@app.get("/login")
def login_page(request: Request, next: str = "/", msg: str = ""):
    return render(request, "login.html", next=safe_next(next), notice=LOGIN_MESSAGES.get(msg, ""))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username, password = str(form.get("username", "")).strip(), str(form.get("password", ""))
    next_url = safe_next(str(form.get("next", "/")))
    with db.session() as conn:
        user = conn.execute(
            "SELECT username FROM users WHERE username = ? AND password = ?", (username, password)
        ).fetchone()
    if user is None:
        return render(request, "login.html", status_code=401, next=next_url, errors=["Invalid credentials"],
                      username=username)
    token = secrets.token_hex(16)
    SESSIONS[token] = {"user": user["username"], "requests": 0}
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@app.get("/logout")
def logout(request: Request):
    SESSIONS.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ---------- infra / demo ----------

@app.get("/health")
def health():
    return JSONResponse({"ok": True})


@app.get("/admin/chaos")
def admin_chaos(request: Request):
    chaos: ChaosConfig = request.app.state.chaos
    return render(request, "admin_chaos.html", chaos=chaos, renames=BUTTON_LABELS)


# ---------- dashboard ----------

@app.get("/")
def dashboard(request: Request, user: str = Depends(require_user)):
    with db.session() as conn:
        counts = {
            "open_pos": conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE status = 'Open'").fetchone()[0],
            "bills": conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0],
            "exceptions": conn.execute("SELECT COUNT(*) FROM exceptions").fetchone()[0],
            "employees": conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0],
            "available_assets": conn.execute("SELECT COUNT(*) FROM equipment WHERE status = 'Available'").fetchone()[0],
            "access_requests": conn.execute("SELECT COUNT(*) FROM access_requests").fetchone()[0],
        }
    return render(request, "dashboard.html", user=user, counts=counts)


# ---------- purchase orders ----------

@app.get("/purchase-orders")
def po_list(request: Request, q: str = "", page: int = 1, user: str = Depends(require_user)):
    like = f"%{q.strip()}%"
    where = "WHERE po.po_number LIKE ? OR v.name LIKE ?"
    with db.session() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id {where}", (like, like)
        ).fetchone()[0]
        pages = max(1, ceil(total / PAGE_SIZE))
        page = min(max(page, 1), pages)
        rows = conn.execute(
            f"SELECT po.*, v.name AS vendor FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id {where}"
            " ORDER BY po.po_number LIMIT ? OFFSET ?",
            (like, like, PAGE_SIZE, (page - 1) * PAGE_SIZE),
        ).fetchall()
    return render(request, "po_list.html", user=user, rows=rows, q=q, page=page, pages=pages, total=total)


@app.get("/purchase-orders/{po_number}")
def po_detail(request: Request, po_number: str, user: str = Depends(require_user)):
    with db.session() as conn:
        po = conn.execute(
            "SELECT po.*, v.name AS vendor FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id"
            " WHERE po.po_number = ?",
            (po_number.upper(),),
        ).fetchone()
    if po is None:
        return render(request, "error.html", status_code=404, user=user, title="Purchase order not found",
                      message=f"No purchase order with number {po_number}.")
    return render(request, "po_detail.html", user=user, po=po)


# ---------- bills ----------

BILL_FIELDS = ("vendor_id", "po_number", "invoice_number", "invoice_date", "amount", "gl_code")
BILL_LABELS = {"vendor_id": "Vendor", "po_number": "PO Number", "invoice_number": "Invoice Number",
               "invoice_date": "Invoice Date", "amount": "Amount", "gl_code": "GL Code"}


def _vendors(conn) -> list:
    return conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()


@app.get("/bills")
def bills_list(request: Request, q: str = "", user: str = Depends(require_user)):
    like = f"%{q.strip()}%"
    with db.session() as conn:
        rows = conn.execute(
            "SELECT b.*, v.name AS vendor FROM bills b JOIN vendors v ON v.id = b.vendor_id"
            " WHERE b.invoice_number LIKE ? OR v.name LIKE ? OR b.bill_number LIKE ? ORDER BY b.bill_number",
            (like, like, like),
        ).fetchall()
    return render(request, "bills_list.html", user=user, rows=rows, q=q)


@app.get("/bills/new")
def bill_form(request: Request, user: str = Depends(require_user)):
    with db.session() as conn:
        vendors = _vendors(conn)
    return render(request, "bill_form.html", user=user, vendors=vendors, values={})


@app.post("/bills/new")
async def bill_create(request: Request, user: str = Depends(require_user)):
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in BILL_FIELDS}
    errors = [f"{BILL_LABELS[k]} is required." for k in BILL_FIELDS if not values[k]]
    amount = db.parse_money(values["amount"]) if values["amount"] else None
    if values["amount"] and amount is None:
        errors.append("Amount must be a number.")
    if values["invoice_date"]:
        try:
            date.fromisoformat(values["invoice_date"])
        except ValueError:
            errors.append("Invoice Date must be a date (YYYY-MM-DD).")

    with db.session() as conn:
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (values["vendor_id"] or None,)).fetchone()
        if values["vendor_id"] and vendor is None:
            errors.append("Unknown vendor.")
        if values["po_number"]:
            po_number = values["po_number"].upper()
            po = conn.execute(
                "SELECT po.*, v.name AS vendor FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id"
                " WHERE po.po_number = ?",
                (po_number,),
            ).fetchone()
            if po is None:
                errors.append(f"PO not found: {po_number}")
            elif vendor is not None and po["vendor_id"] != vendor["id"]:
                errors.append(f"{po_number} belongs to {po['vendor']}, not {vendor['name']}.")

        if errors:
            return render(request, "bill_form.html", status_code=400, user=user, vendors=_vendors(conn),
                          values=values, errors=errors)

        bill_number = db.next_number(conn, "bills", "bill_number", "BILL")
        conn.execute(
            "INSERT INTO bills (bill_number, vendor_id, po_number, invoice_number, invoice_date, amount, gl_code,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (bill_number, vendor["id"], values["po_number"].upper(), values["invoice_number"],
             values["invoice_date"], amount, values["gl_code"], db.now()),
        )
    return RedirectResponse(f"/bills/{bill_number}?created=1", status_code=303)


@app.get("/bills/{bill_number}")
def bill_detail(request: Request, bill_number: str, created: int = 0, user: str = Depends(require_user)):
    with db.session() as conn:
        bill = conn.execute(
            "SELECT b.*, v.name AS vendor FROM bills b JOIN vendors v ON v.id = b.vendor_id WHERE b.bill_number = ?",
            (bill_number.upper(),),
        ).fetchone()
    if bill is None:
        return render(request, "error.html", status_code=404, user=user, title="Bill not found",
                      message=f"No bill with number {bill_number}.")
    flash = f"Bill {bill['bill_number']} created" if created else ""
    return render(request, "bill_detail.html", user=user, bill=bill, flash=flash)


# ---------- AP exceptions ----------

@app.get("/exceptions")
def exceptions_list(request: Request, created: str = "", user: str = Depends(require_user)):
    with db.session() as conn:
        rows = conn.execute(
            "SELECT e.*, v.name AS vendor FROM exceptions e JOIN vendors v ON v.id = e.vendor_id"
            " ORDER BY e.exception_number"
        ).fetchall()
    flash = f"Exception {created} logged" if created else ""
    return render(request, "exceptions_list.html", user=user, rows=rows, flash=flash)


@app.get("/exceptions/new")
def exception_form(request: Request, user: str = Depends(require_user)):
    with db.session() as conn:
        vendors = _vendors(conn)
    return render(request, "exception_form.html", user=user, vendors=vendors, values={})


@app.post("/exceptions/new")
async def exception_create(request: Request, user: str = Depends(require_user)):
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in ("vendor_id", "invoice_number", "po_number", "reason")}
    errors = [f"{name} is required." for key, name in
              (("vendor_id", "Vendor"), ("invoice_number", "Invoice Number"), ("reason", "Reason"))
              if not values[key]]
    with db.session() as conn:
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (values["vendor_id"] or None,)).fetchone()
        if values["vendor_id"] and vendor is None:
            errors.append("Unknown vendor.")
        if errors:
            return render(request, "exception_form.html", status_code=400, user=user, vendors=_vendors(conn),
                          values=values, errors=errors)
        number = db.next_number(conn, "exceptions", "exception_number", "EXC")
        conn.execute(
            "INSERT INTO exceptions (exception_number, vendor_id, invoice_number, po_number, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (number, vendor["id"], values["invoice_number"], values["po_number"].upper() or None,
             values["reason"], db.now()),
        )
    return RedirectResponse(f"/exceptions?created={number}", status_code=303)


# ---------- vendors ----------

@app.get("/vendors")
def vendors_list(request: Request, user: str = Depends(require_user)):
    with db.session() as conn:
        rows = _vendors(conn)
    return render(request, "vendors.html", user=user, rows=rows)


# ---------- employees ----------

EMPLOYEE_FIELDS = (("full_name", "Full Name"), ("email", "Email"), ("department", "Department"),
                   ("title", "Title"), ("manager_name", "Manager"), ("start_date", "Start Date"))


def _employees(conn) -> list:
    return conn.execute("SELECT * FROM employees ORDER BY full_name").fetchall()


@app.get("/employees")
def employees_list(request: Request, user: str = Depends(require_user)):
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM employees ORDER BY employee_id").fetchall()
    return render(request, "employees_list.html", user=user, rows=rows)


@app.get("/employees/new")
def employee_form(request: Request, user: str = Depends(require_user)):
    with db.session() as conn:
        managers = _employees(conn)
    return render(request, "employee_form.html", user=user, managers=managers, departments=DEPARTMENTS, values={})


@app.post("/employees/new")
async def employee_create(request: Request, user: str = Depends(require_user)):
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k, _ in EMPLOYEE_FIELDS}
    errors = [f"{name} is required." for key, name in EMPLOYEE_FIELDS if not values[key]]
    if values["email"] and "@" not in values["email"]:
        errors.append("Email must be a valid email address.")
    if values["department"] and values["department"] not in DEPARTMENTS:
        errors.append("Unknown department.")
    if values["start_date"]:
        try:
            date.fromisoformat(values["start_date"])
        except ValueError:
            errors.append("Start Date must be a date (YYYY-MM-DD).")
    with db.session() as conn:
        if values["manager_name"] and not conn.execute(
            "SELECT 1 FROM employees WHERE full_name = ?", (values["manager_name"],)
        ).fetchone():
            errors.append("Manager must be an existing employee.")
        if values["email"] and conn.execute(
            "SELECT 1 FROM employees WHERE lower(email) = lower(?)", (values["email"],)
        ).fetchone():
            errors.append(f"An employee with email {values['email']} already exists.")
        if errors:
            return render(request, "employee_form.html", status_code=400, user=user, managers=_employees(conn),
                          departments=DEPARTMENTS, values=values, errors=errors)
        employee_id = db.next_number(conn, "employees", "employee_id", "EMP")
        conn.execute(
            "INSERT INTO employees (employee_id, full_name, email, department, title, manager_name, start_date,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (employee_id, values["full_name"], values["email"].lower(), values["department"], values["title"],
             values["manager_name"], values["start_date"], db.now()),
        )
    return RedirectResponse(f"/employees/{employee_id}?created=1", status_code=303)


@app.get("/employees/{employee_id}")
def employee_detail(request: Request, employee_id: str, created: int = 0, user: str = Depends(require_user)):
    with db.session() as conn:
        emp = conn.execute("SELECT * FROM employees WHERE employee_id = ?", (employee_id.upper(),)).fetchone()
        if emp is None:
            return render(request, "error.html", status_code=404, user=user, title="Employee not found",
                          message=f"No employee with id {employee_id}.")
        assets = conn.execute("SELECT * FROM equipment WHERE assigned_to_employee_id = ?", (emp["id"],)).fetchall()
        access = conn.execute("SELECT * FROM access_requests WHERE employee_id = ? ORDER BY request_number",
                              (emp["id"],)).fetchall()
    flash = f"Employee {emp['employee_id']} created" if created else ""
    return render(request, "employee_detail.html", user=user, emp=emp, assets=assets, access=access, flash=flash)


# ---------- equipment ----------

def _asset(conn, asset_tag: str):
    return conn.execute(
        "SELECT eq.*, e.full_name AS assigned_to, e.employee_id AS assigned_employee_id FROM equipment eq"
        " LEFT JOIN employees e ON e.id = eq.assigned_to_employee_id WHERE eq.asset_tag = ?",
        (asset_tag.upper(),),
    ).fetchone()


@app.get("/equipment")
def equipment_list(request: Request, user: str = Depends(require_user)):
    with db.session() as conn:
        rows = conn.execute(
            "SELECT eq.*, e.full_name AS assigned_to FROM equipment eq"
            " LEFT JOIN employees e ON e.id = eq.assigned_to_employee_id ORDER BY eq.asset_tag"
        ).fetchall()
    return render(request, "equipment_list.html", user=user, rows=rows)


@app.get("/equipment/{asset_tag}")
def equipment_detail(request: Request, asset_tag: str, assigned: int = 0, user: str = Depends(require_user)):
    with db.session() as conn:
        asset = _asset(conn, asset_tag)
        if asset is None:
            return render(request, "error.html", status_code=404, user=user, title="Asset not found",
                          message=f"No asset with tag {asset_tag}.")
        employees = _employees(conn)
    flash = f"{asset['asset_tag']} assigned to {asset['assigned_to']}" if assigned and asset["assigned_to"] else ""
    return render(request, "equipment_detail.html", user=user, asset=asset, employees=employees, flash=flash,
                  values={})


@app.post("/equipment/{asset_tag}")
async def equipment_assign(request: Request, asset_tag: str, user: str = Depends(require_user)):
    form = await request.form()
    employee_pk = str(form.get("employee_id", "")).strip()
    with db.session() as conn:
        asset = _asset(conn, asset_tag)
        if asset is None:
            return render(request, "error.html", status_code=404, user=user, title="Asset not found",
                          message=f"No asset with tag {asset_tag}.")
        errors = []
        if asset["status"] != "Available":
            errors.append(f"Asset {asset['asset_tag']} is not available (status: {asset['status']}).")
        employee = conn.execute("SELECT * FROM employees WHERE id = ?", (employee_pk or None,)).fetchone()
        if employee is None:
            errors.append("Employee is required.")
        if errors:
            return render(request, "equipment_detail.html", status_code=409, user=user, asset=asset,
                          employees=_employees(conn), values={"employee_id": employee_pk}, errors=errors)
        conn.execute(
            "UPDATE equipment SET status = 'Assigned', assigned_to_employee_id = ? WHERE id = ?",
            (employee["id"], asset["id"]),
        )
    return RedirectResponse(f"/equipment/{asset['asset_tag']}?assigned=1", status_code=303)


# ---------- access requests ----------

@app.get("/access-requests")
def access_list(request: Request, created: str = "", user: str = Depends(require_user)):
    with db.session() as conn:
        rows = conn.execute(
            "SELECT ar.*, e.full_name AS employee FROM access_requests ar"
            " JOIN employees e ON e.id = ar.employee_id ORDER BY ar.request_number"
        ).fetchall()
    flash = f"Access request {created} submitted" if created else ""
    return render(request, "access_list.html", user=user, rows=rows, flash=flash)


@app.get("/access-requests/new")
def access_form(request: Request, user: str = Depends(require_user)):
    with db.session() as conn:
        employees = _employees(conn)
    return render(request, "access_form.html", user=user, employees=employees, systems=SYSTEMS,
                  levels=ACCESS_LEVELS, values={})


@app.post("/access-requests/new")
async def access_create(request: Request, user: str = Depends(require_user)):
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in ("employee_id", "system_name", "access_level")}
    errors = []
    with db.session() as conn:
        employee = conn.execute("SELECT * FROM employees WHERE id = ?", (values["employee_id"] or None,)).fetchone()
        if employee is None:
            errors.append("Employee is required.")
        if values["system_name"] not in SYSTEMS:
            errors.append("System is required.")
        if values["access_level"] not in ACCESS_LEVELS:
            errors.append("Access Level is required.")
        if errors:
            return render(request, "access_form.html", status_code=400, user=user, employees=_employees(conn),
                          systems=SYSTEMS, levels=ACCESS_LEVELS, values=values, errors=errors)
        number = db.next_number(conn, "access_requests", "request_number", "AR")
        conn.execute(
            "INSERT INTO access_requests (request_number, employee_id, system_name, access_level, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (number, employee["id"], values["system_name"], values["access_level"], db.now()),
        )
    return RedirectResponse(f"/access-requests?created={number}", status_code=303)
