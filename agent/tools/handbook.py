"""Handbook tools: the company's SOPs, policies and system directory.

No vector search on purpose: with a handful of documents the model picks the right one
from the index (titles, descriptions, example triggers). At hundreds of documents,
list_handbook would become a semantic search.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from agent.config import Config
from agent.models import ToolResult
from agent.tools.base import Tool

if TYPE_CHECKING:
    from agent.tools.base import RunContext


def split_front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        _, meta, body = text.split("---\n", 2)
        return yaml.safe_load(meta) or {}, body.strip()
    return {}, text.strip()


def handbook_index(config: Config) -> list[dict]:
    entries = []
    for path in sorted(config.sops_dir.glob("*.md")):
        meta, _ = split_front_matter(path.read_text())
        entries.append({"name": path.stem, "title": meta.get("title", path.stem),
                        "description": meta.get("description", ""), "triggers": meta.get("triggers", [])})
    return entries


def read_sop(config: Config, name: str) -> tuple[str, str] | None:
    """Find an SOP by file name or title (case-insensitive). Returns (name, full text)."""
    wanted = name.lower().removesuffix(".md")
    for path in sorted(config.sops_dir.glob("*.md")):
        meta, _ = split_front_matter(path.read_text())
        if wanted in (path.stem.lower(), str(meta.get("title", "")).lower()):
            return path.stem, path.read_text()
    return None


def policies_text(config: Config) -> str:
    lines = []
    for rule in config.policies.get("rules", []):
        w = rule["when"]
        lines.append(f"- {rule['id']}: {rule['description']} "
                     f"[{w['action']} where {w['field']} {w['op']} {w['value']} -> {rule['effect']}]")
    for check in config.policies.get("checks", []):
        lines.append(f"- {check['id']}: {check['description']} [checked in the system by the gate before every "
                     f"{check['when']['action']}; a failing submission is blocked]")
    if config.policies.get("unknown_task", {}).get("require_plan_approval"):
        lines.append("- Tasks with no matching SOP: the plan must be approved by a human before acting.")
    return "\n".join(lines)


def systems_overview(config: Config) -> str:
    lines = []
    for name, system in config.systems.items():
        if system.get("kind") == "web":
            lines.append(f"- {name} (web, {config.base_url(system=name)}): {system.get('description', '').strip()}")
            lines.extend(f"    - {area}" for area in system.get("areas", []))
        else:
            lines.append(f"- {name} (folder '{system['path']}', {system.get('access', 'read')}): {system.get('description', '')}")
    return "\n".join(lines)


class ListHandbook(Tool):
    name = "list_handbook"
    description = "List the company's procedures (SOPs) with their titles, descriptions and example requests."

    def run(self, ctx) -> ToolResult:
        return ToolResult.success(handbook_index(ctx.config))


class ReadHandbook(Tool):
    name = "read_handbook"
    description = "Read one procedure (SOP) in full, by its name or title from list_handbook."
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def run(self, ctx, name: str) -> ToolResult:
        found = read_sop(ctx.config, name)
        if found is None:
            names = [e["name"] for e in handbook_index(ctx.config)]
            return ToolResult.fail("not_found", f"No procedure named '{name}'. Available: {names}.")
        return ToolResult.success({"name": found[0], "text": found[1]})


class GetPolicies(Tool):
    name = "get_policies"
    description = "List the company policies that govern actions (approvals required, forbidden actions)."

    def run(self, ctx) -> ToolResult:
        return ToolResult.success({"policies": policies_text(ctx.config)})


HANDBOOK_TOOLS = [ListHandbook, ReadHandbook, GetPolicies]
