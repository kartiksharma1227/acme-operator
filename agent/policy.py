"""The policy gate: plain code, not the model, decides whether a submission may happen.

Before any click that submits a form, the gate
  1. recognises the action from the page URL (action signatures in the system directory),
  2. reads the values the form is about to submit straight from the page,
  3. evaluates the company policy rules against those values,
  4. confirms any pre-submit checks the policies declare by looking them up itself (agent/checks.py),
and then allows it, blocks it, or pauses for a human. The model's own description of
what it is doing is never trusted for this decision.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from agent.checks import browser_fetch, evaluate, parse_number
from agent.config import Config
from agent.models import ToolResult

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from agent.tools.base import RunContext

OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "in": lambda a, b: a in b,
}


@dataclass
class ActionDescriptor:
    system: str
    action_type: str
    risk: str
    params: dict[str, Any] = field(default_factory=dict)


def _normalise(value: Any) -> str:
    return str(value).strip().lower()


NUMBER_LIKE = re.compile(r"\s*\$?\s*-?[\d,]*\.?\d+\s*")


def canonical(value: Any) -> Any:
    """Compare values however they were typed: '$12,800.00' == '12800', ' Admin ' == 'admin'."""
    if isinstance(value, str):
        if NUMBER_LIKE.fullmatch(value):
            return parse_number(value)
        return _normalise(value)
    return value


def canonical_params(params: dict) -> dict:
    return {k: canonical(v) for k, v in sorted(params.items())}


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def rule_matches(when: dict, params: dict) -> bool:
    actual, expected, op = params.get(when["field"]), when["value"], OPS[when["op"]]
    if actual is None:
        return False
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        number = parse_number(actual)
        return number is not None and op(number, float(expected))
    if isinstance(expected, list):
        return op(_normalise(actual), [_normalise(e) for e in expected])
    return op(_normalise(actual), _normalise(expected))


def grant_key(descriptor: ActionDescriptor, rule_id: str) -> str:
    return _digest({"action": descriptor.action_type, "params": canonical_params(descriptor.params), "rule": rule_id})


def read_field(page: "Page", label: str) -> str | None:
    field_ = page.get_by_label(label, exact=True)
    if field_.count() == 0:
        return None
    element = field_.first
    if element.evaluate("el => el.tagName") == "SELECT":
        # A placeholder option ("— Select … —") has an empty value: nothing has been chosen.
        return element.evaluate("el => el.selectedOptions.length && el.selectedOptions[0].value !== ''"
                                " ? el.selectedOptions[0].text.trim() : ''")
    return element.input_value()


class PolicyGate:
    def __init__(self, config: Config):
        self.signatures = [
            (system, action, re.compile(action["url_pattern"]))
            for system, spec in config.systems.items()
            for action in spec.get("actions", [])
            if action.get("trigger") == "form_submit"
        ]
        self.rules = config.policies.get("rules", [])
        self.checks = config.policies.get("checks", [])
        self.risk_defaults = config.policies.get("risk_defaults", {})
        self.idempotency_fields = {action["type"]: action.get("idempotency_key", []) for _, action, _ in self.signatures}
        self._pending: tuple[ActionDescriptor, str | None] | None = None  # allowed, not yet known to have succeeded

    def idempotency_key(self, d: ActionDescriptor) -> str | None:
        fields = self.idempotency_fields.get(d.action_type)
        if not fields:
            return None
        return _digest({"action": d.action_type, **{f: canonical(d.params.get(f)) for f in fields}})

    def record(self, ctx: "RunContext", result: ToolResult) -> None:
        """Called after the submit click. Only a successful submission enters the ledger, so a
        rejected or interrupted attempt can be retried."""
        pending, self._pending = self._pending, None
        data = result.data if isinstance(result.data, dict) else {}
        # Accepted-but-unconfirmed (e.g. the session expired on the confirmation page) still counts as done.
        if pending is None or not (result.ok or data.get("submission_accepted")):
            return
        descriptor, key = pending
        evidence = " · ".join(filter(None, [*data.get("notices", []),
                                            data.get("submission_accepted") or urlparse(data.get("url", "")).path]))
        entry = {"key": key, "action": descriptor.action_type, "params": descriptor.params, "evidence": evidence}
        if key:
            ctx.state.action_ledger.append(entry)
        ctx.emit("action_committed", entry)

    def __call__(self, ctx: "RunContext", page: "Page", target) -> ToolResult | None:
        self._pending = None
        descriptor = self.describe(page)
        if descriptor is None:
            # Not a recognised business action (e.g. a sign-in or search form): allowed, but on record.
            ctx.emit("policy_check", {"action": "unclassified_write", "url": page.url, "decision": "allow"})
            return None
        return self.decide(ctx, descriptor, browser_fetch(page))

    def describe(self, page: "Page") -> ActionDescriptor | None:
        path = urlparse(page.url).path
        for system, action, pattern in self.signatures:
            if match := pattern.match(path):
                params = {name: read_field(page, label) for name, label in action.get("fields", {}).items()}
                params.update(match.groupdict())
                return ActionDescriptor(system, action["type"], action.get("risk", "write"), params)
        return None

    def decide(self, ctx: "RunContext", d: ActionDescriptor, fetch=None) -> ToolResult | None:
        matched = [r for r in self.rules if r["when"]["action"] == d.action_type and rule_matches(r["when"], d.params)]
        base = {"system": d.system, "action": d.action_type, "params": d.params, "rules": [r["id"] for r in matched]}

        # Every field a signature names is essential to the action: never judge (or ask a human to
        # approve) a half-filled form. The values the policies need simply aren't there yet.
        empty = [name for name, value in d.params.items() if not str(value or "").strip()]
        if empty:
            ctx.emit("policy_check", {**base, "decision": "incomplete", "empty": empty})
            return ToolResult.fail("validation", f"The form is incomplete ({', '.join(empty)} not filled in), so "
                                                 "nothing was submitted. Fill in every field, then submit again.")

        # Never do the same thing twice, even if the model loses track (e.g. after a resume).
        key = self.idempotency_key(d)
        done = next((e for e in ctx.state.action_ledger if key and e["key"] == key), None)
        if done:
            ctx.emit("policy_check", {**base, "decision": "duplicate_blocked", "previous": done["evidence"]})
            return ToolResult.fail("policy_denied", f"Already done earlier in this run ({done['evidence']}). "
                                                    "Nothing was submitted. Do not repeat it; use the existing result.")

        for rule in matched:
            if rule["effect"] == "deny":
                ctx.emit("policy_check", {**base, "decision": "deny", "rule": rule["id"]})
                return ToolResult.fail("policy_denied", f"Blocked by policy {rule['id']}: {rule['description']} "
                                                        "Nothing was submitted. Do not attempt this action; record "
                                                        "the item as blocked by policy and continue.")

        # Facts the submission depends on, confirmed in the system itself before anyone is asked to approve.
        checked = []
        for check in (c for c in self.checks if fetch and c["when"]["action"] == d.action_type):
            result = evaluate(check, d.params, fetch)
            checked.append({"id": result.id, "passed": result.passed, "detail": result.detail})
            if result.unavailable:
                ctx.emit("policy_check", {**base, "decision": "check_unavailable", "rule": result.id, "detail": result.detail})
                return ToolResult.fail("auth" if result.unavailable == "auth" else "transient",
                                       f"Could not run pre-submit check {result.id} ({result.detail}), so nothing was "
                                       "submitted. If you were signed out, sign in again; then return to the form, "
                                       "fill it in again and resubmit.")
            if not result.passed:
                ctx.state.bump("checks_blocked")
                ctx.emit("policy_check", {**base, "decision": "check_failed", "rule": result.id, "detail": result.detail})
                return ToolResult.fail("policy_denied", f"Blocked by pre-submit check {result.id}: {check['description']} "
                                                        f"Found: {result.detail}. Nothing was submitted. Re-check the "
                                                        "source document against the system and follow the procedure "
                                                        "for this case; this item probably needs a different outcome. "
                                                        "Do not try to get around the check.")
        if checked:
            base["checks"] = checked

        approvals = [r for r in matched if r["effect"] == "require_approval"]
        if not approvals and self.risk_defaults.get(d.risk) == "require_approval":
            approvals = [{"id": f"default-{d.risk}", "description": f"'{d.risk}' actions require approval by default."}]

        for rule in approvals:
            grant = grant_key(d, rule["id"])
            if grant in ctx.state.grants:
                continue
            payload = {
                "summary": f"{d.action_type.replace('_', ' ')} in {d.system}",
                "rule_id": rule["id"], "rule": rule["description"], "params": d.params,
                "reason": next((s.title for s in ctx.state.plan if s.status == "in_progress"), ""),
            }
            ctx.emit("approval_requested", payload)
            response = ctx.human.ask("approval", payload)
            ctx.state.bump("human_approvals")
            ctx.emit("approval_decided", {**payload, "approved": bool(response.approved), "note": response.note})
            if not response.approved:
                ctx.emit("policy_check", {**base, "decision": "denied_by_human", "rule": rule["id"]})
                note = f" ({response.note})" if response.note else ""
                return ToolResult.fail("policy_denied", f"A human denied approval under {rule['id']}{note}. Nothing "
                                                        "was submitted. Do not retry; record this item as not done "
                                                        "because approval was denied, and continue.")
            ctx.state.grants.append(grant)

        ctx.emit("policy_check", {**base, "decision": "allow"})
        self._pending = (d, key)
        return None
