"""Tool interface, run context and registry.

Every tool returns a ToolResult; the registry guarantees that, even when a tool raises,
so the agent loop never crashes because of a tool. Errors come back typed
(transient, not_found, auth, ...) with a message saying how to recover.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agent.config import Config
from agent.human import HumanChannel, RunPaused
from agent.models import Risk, RunState, ToolResult

if TYPE_CHECKING:
    from agent.tools.browser import BrowserSession

Emit = Callable[[str, dict], None]
# The policy gate (Phase 5) plugs in here: (ctx, page, locator) -> None to allow, or a ToolResult to block.
Gate = Callable[..., "ToolResult | None"]


@dataclass
class RunContext:
    config: Config
    state: RunState
    run_dir: Path
    human: HumanChannel
    emit: Emit = lambda event_type, data: None
    gate: Gate | None = None
    secrets: set[str] = field(default_factory=set)  # real secret values, for the redaction backstop
    _browser: "BrowserSession | None" = None

    @property
    def browser(self) -> "BrowserSession":
        if self._browser is None:
            from agent.tools.browser import BrowserSession
            self._browser = BrowserSession(self)
        return self._browser

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None


class Tool:
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}, "required": []}
    risk: Risk = "read"
    read_only: bool = True  # only read-only tools are given to the verifier
    changes_view: bool = False  # replaces what the operator is looking at (e.g. loads another page)

    def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        raise NotImplementedError

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}


def redact(value: Any, secrets: set[str]) -> Any:
    """Replace any known secret value inside strings/containers with ***."""
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "***")
        return value
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    return value


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self.tools = {t.name: t for t in tools}

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def read_only(self) -> "ToolRegistry":
        return ToolRegistry([t for t in self.tools.values() if t.read_only])

    def names(self) -> list[str]:
        return list(self.tools)

    def run(self, ctx: RunContext, name: str, arguments: str | dict | None) -> ToolResult:
        result = self._run(ctx, name, arguments)
        event = {"tool": name, "ok": result.ok, "error_type": result.error_type,
                 "message": result.message, "evidence": result.evidence}
        if isinstance(result.data, dict):  # where we ended up and what the system said, for the audit trail
            event.update({k: result.data[k] for k in ("url", "status", "alerts", "notices") if result.data.get(k)})
        ctx.emit("tool_result", redact(event, ctx.secrets))
        return result

    def _run(self, ctx: RunContext, name: str, arguments: str | dict | None) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult.fail("fatal", f"Unknown tool '{name}'. Available tools: {', '.join(self.tools)}.")

        if isinstance(arguments, dict):
            args = arguments
        else:
            try:
                args = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                return ToolResult.fail("validation", f"Arguments for {name} were not valid JSON ({exc}). Retry with a JSON object.")
        args = {k: v for k, v in args.items() if k}  # models occasionally send a stray empty-named argument
        ctx.emit("tool_call", {"tool": name, "args": redact(args, ctx.secrets)})

        props = tool.parameters.get("properties", {})
        missing = [p for p in tool.parameters.get("required", []) if p not in args]
        unknown = [a for a in args if a not in props]
        if missing or unknown:
            return ToolResult.fail("validation", f"Bad arguments for {name}: missing {missing or 'none'}, "
                                                 f"unexpected {unknown or 'none'}. Expected parameters: {list(props)}.")
        try:
            return tool.run(ctx, **args)
        except RunPaused:
            raise  # waiting for a human is control flow, not a tool failure
        except Exception as exc:  # the loop must survive any tool bug
            return ToolResult.fail("fatal", f"{name} failed unexpectedly: {type(exc).__name__}: {exc}")
