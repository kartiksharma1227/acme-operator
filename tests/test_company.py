"""The company handbook must be internally consistent and match the real ERP forms.

A typo here (wrong label, unknown action, bad regex) wouldn't crash anything; it would
silently make the policy gate miss an action. So we check it up front.
"""
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sandbox import app as erp
from sandbox import seed
from sandbox.chaos import ChaosConfig

ROOT = Path(__file__).resolve().parent.parent
COMPANY = ROOT / "company"
SYSTEMS = yaml.safe_load((COMPANY / "systems.yaml").read_text())["systems"]
POLICIES = yaml.safe_load((COMPANY / "policies.yaml").read_text())
ACTIONS = {a["type"]: a for s in SYSTEMS.values() for a in s.get("actions", [])}

# A concrete ERP page for each action signature, used to check the labels really exist.
SAMPLE_PAGES = {
    "create_bill": "/bills/new",
    "log_exception": "/exceptions/new",
    "create_employee": "/employees/new",
    "assign_equipment": "/equipment/MBP-0012",
    "request_access": "/access-requests/new",
}


def url_groups(action: dict) -> set[str]:
    return set(re.compile(action["url_pattern"]).groupindex)


def front_matter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.name} has no front-matter"
    return yaml.safe_load(text.split("---\n", 2)[1])


def test_action_signatures_are_well_formed():
    assert ACTIONS, "no action signatures defined"
    for name, action in ACTIONS.items():
        re.compile(action["url_pattern"])
        assert action["trigger"] == "form_submit"
        assert action["risk"] in {"read", "write", "irreversible"}
        known = set(action["fields"]) | url_groups(action)
        assert set(action["idempotency_key"]) <= known, f"{name}: idempotency key uses unknown params"


def test_policy_rules_reference_real_actions_and_fields():
    for rule in POLICIES["rules"]:
        when = rule["when"]
        assert when["action"] in ACTIONS, f"{rule['id']}: unknown action {when['action']}"
        action = ACTIONS[when["action"]]
        assert when["field"] in set(action["fields"]) | url_groups(action), f"{rule['id']}: unknown field"
        assert when["op"] in {">", ">=", "<", "<=", "==", "!=", "in"}
        assert rule["effect"] in {"allow", "require_approval", "deny"}
    assert set(POLICIES["risk_defaults"]) == {"read", "write", "irreversible"}


def test_folder_systems_exist():
    for name, system in SYSTEMS.items():
        if system.get("kind") == "folder":
            assert (ROOT / system["path"]).is_dir(), f"{name}: missing folder {system['path']}"
            assert system["access"] in {"read", "write"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_DB", str(tmp_path / "acme.db"))
    seed.reset(tmp_path / "acme.db")
    erp.app.state.chaos = ChaosConfig()
    c = TestClient(erp.app)
    c.post("/login", data={"username": "agent", "password": "acme-demo-123", "next": "/"})
    return c


@pytest.mark.parametrize("action_type", sorted(SAMPLE_PAGES))
def test_action_labels_exist_on_the_erp_form(client, action_type):
    action, path = ACTIONS[action_type], SAMPLE_PAGES[action_type]
    assert re.match(action["url_pattern"], path), f"{path} doesn't match {action['url_pattern']}"
    html = client.get(path).text
    for label in action["fields"].values():
        assert re.search(rf"<label for=\"[^\"]+\">{re.escape(label)}</label>", html), \
            f"{action_type}: no '{label}' label on {path}"


def test_sops_have_front_matter():
    sops = sorted((COMPANY / "sops").glob("*.md"))
    assert len(sops) >= 2
    for path in sops:
        meta = front_matter(path)
        assert meta.get("title") and meta.get("description"), path.name
        assert isinstance(meta.get("triggers"), list) and meta["triggers"], path.name


def test_sops_only_cite_policies_that_exist():
    rule_ids = {r["id"] for r in POLICIES["rules"]}
    for path in (COMPANY / "sops").glob("*.md"):
        cited = set(re.findall(r"\b([A-Z]{2,4}-\d+)\b", path.read_text()))
        cited = {c for c in cited if not c.startswith(("PO-", "INV-", "EMP-", "MBP-", "MBA-"))}
        assert cited <= rule_ids, f"{path.name} cites unknown policies {cited - rule_ids}"
