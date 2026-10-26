"""Keep the model's context small without losing facts.

Page snapshots are the bulk of the conversation and go stale once the operator has seen
them and acted again. Before each model call, older tool results lose only their snapshot;
their outcome (ok / error, URL, status, alerts, notices such as "record X created") stays.
Results the model has not seen yet are never trimmed. Messages are shrunk, never removed,
so every tool call keeps its matching result.
"""
from __future__ import annotations

import json

OMITTED = "[snapshot omitted: superseded by a later observation]"
# Small pages (a single record's details) cost little and hold the facts decisions are made on,
# so only large snapshots (lists, long pages) are ever trimmed.
SMALL_SNAPSHOT_CHARS = 1500


def _has_snapshot(message: dict) -> bool:
    content = message.get("content") or ""
    return message.get("role") == "tool" and '"page":' in content and len(content) > SMALL_SNAPSHOT_CHARS


def _strip_snapshot(content: str) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content
    data = payload.get("data")
    if isinstance(data, dict) and "page" in data:
        payload["data"] = {**data, "page": OMITTED}
    return json.dumps(payload, ensure_ascii=False)


def trim(messages: list[dict], keep_full: int = 2) -> list[dict]:
    """A copy of `messages` with stale snapshots stripped.

    A snapshot is stale once the model has already responded to it (it comes before the latest
    assistant turn), except for the `keep_full` most recent of those."""
    last_turn = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
    seen = [i for i, m in enumerate(messages) if _has_snapshot(m) and i < last_turn]
    stale = set(seen[:-keep_full] if keep_full else seen)
    return [{**m, "content": _strip_snapshot(m["content"])} if i in stale else m for i, m in enumerate(messages)]
