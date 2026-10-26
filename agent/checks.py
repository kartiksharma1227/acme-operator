"""Pre-submit checks: facts the gate confirms in the system of record itself before a write.

Some mistakes can't be undone once submitted (the target system may have no delete), so for
those the company declares checks in its policies. Before a matching submission the gate
looks the fact up, read-only, in a separate tab of the same signed-in browser session, and
blocks the submission if it isn't true. If it can't look, nothing is submitted (fail closed).

Two kinds, both driven entirely by the policy file:
  must_not_exist: {row_contains: [field, ...]}   a lookup list must have no row holding all these values
  must_match: [{shown_as, field | equals, tolerance?}, ...]
      each value shown on the lookup page must equal the form's `field` (numbers within `tolerance`)
      or the constant `equals`
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote, urljoin, urlparse

if TYPE_CHECKING:
    from playwright.sync_api import Page

RETRIES = (0.5, 1.0, 2.0)
LOOKUP_TIMEOUT_MS = 15_000

# A shown value is read from a definition list (<dt>Label</dt><dd>value</dd>) or a row header.
SHOWN_VALUE_JS = """(label) => {
  const norm = s => s.replace(/\\s+/g, ' ').trim().toLowerCase();
  for (const el of document.querySelectorAll('dt, th[scope="row"]')) {
    if (norm(el.textContent) === norm(label) && el.nextElementSibling) return el.nextElementSibling.textContent.trim();
  }
  return null;
}"""


def parse_number(value: Any) -> float | None:
    """'$12,800.00' -> 12800.0; anything non-numeric -> None."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


@dataclass
class Fetched:
    status: str                      # ok | not_found | auth | unavailable
    rows: list[str] = field(default_factory=list)
    shown: dict[str, str | None] = field(default_factory=dict)  # label -> value shown next to it
    detail: str = ""


Fetch = Callable[[str, list[str]], Fetched]


@dataclass
class CheckResult:
    id: str
    passed: bool
    detail: str
    unavailable: str = ""            # set when the check could not run (auth / unavailable)


def lookup_path(template: str, params: dict) -> str | None:
    """'/items?q={ref}' filled from the form values; None if a value it needs is empty."""
    names = [part.split("}")[0] for part in template.split("{")[1:]]
    if any(not str(params.get(n) or "").strip() for n in names):
        return None
    return template.format(**{n: quote(str(params[n]).strip(), safe="") for n in names})


def evaluate(check: dict, params: dict, fetch: Fetch) -> CheckResult:
    path = lookup_path(check["lookup"], params)
    if path is None:
        return CheckResult(check["id"], False, f"the form is missing a value needed to look up {check['lookup']}")
    conditions = check.get("must_match") or []
    page = fetch(path, [c["shown_as"] for c in conditions])
    if page.status in ("auth", "unavailable"):
        return CheckResult(check["id"], False, page.detail, unavailable=page.status)

    if rule := check.get("must_not_exist"):
        wanted = [str(params.get(f) or "").strip().lower() for f in rule["row_contains"]]
        hits = [r for r in page.rows if all(w in r.lower() for w in wanted)]
        if hits:
            return CheckResult(check["id"], False, f"{path} already shows a matching record: {' '.join(hits[0].split())}")
        return CheckResult(check["id"], True, f"no matching record at {path}")

    if conditions:
        if page.status == "not_found":
            return CheckResult(check["id"], False, f"no record at {path}")
        findings = [_compare(c, params, page.shown.get(c["shown_as"])) for c in conditions]
        detail = f"at {path}: " + "; ".join(text for _, text in findings)
        return CheckResult(check["id"], all(ok for ok, _ in findings), detail)
    raise ValueError(f"check {check['id']} has no must_not_exist / must_match")


def _compare(condition: dict, params: dict, shown: str | None) -> tuple[bool, str]:
    label = condition["shown_as"]
    if shown is None:
        return False, f"'{label}' is not shown"
    if "equals" in condition:
        expected, what = str(condition["equals"]), "required"
    else:
        expected, what = str(params.get(condition["field"]) or ""), f"form {condition['field']}"
    if "tolerance" in condition:
        a, b = parse_number(expected), parse_number(shown)
        if a is None or b is None:
            return False, f"cannot compare {label} {shown!r} with {what} {expected!r}"
        difference = round(abs(a - b), 2)
        ok = difference <= float(condition["tolerance"])
        return ok, f"{label} {b:,.2f} vs {what} {a:,.2f} (difference {difference:,.2f}{'' if ok else ', too large'})"
    ok = " ".join(shown.split()).lower() == " ".join(expected.split()).lower()
    return ok, f"{label} is {shown!r}" + ("" if ok else f", {what} is {expected!r}")


def browser_fetch(page: "Page") -> Fetch:
    """Look things up in a separate tab of the operator's own session; its form is left untouched."""
    def fetch(path: str, labels: list[str]) -> Fetched:
        url = urljoin(page.url, path)
        probe = page.context.new_page()
        try:
            for delay in (*RETRIES, None):
                try:
                    response = probe.goto(url, timeout=LOOKUP_TIMEOUT_MS)
                except Exception as exc:  # timeouts, connection errors
                    status, detail = None, f"{type(exc).__name__}"
                else:
                    status, detail = (response.status if response else None), ""
                if status is not None and status < 500:
                    break
                if delay is None:
                    return Fetched("unavailable", detail=f"lookup of {path} kept failing ({status or detail})")
                time.sleep(delay)
            if urlparse(probe.url).path.rstrip("/").endswith("/login"):
                return Fetched("auth", detail=f"signed out while looking up {path}")
            if status == 404:
                return Fetched("not_found")
            rows = probe.get_by_role("row").all_inner_texts()
            shown = {label: probe.evaluate(SHOWN_VALUE_JS, label) for label in labels}
            return Fetched("ok", rows=rows, shown=shown)
        finally:
            probe.close()
    return fetch
