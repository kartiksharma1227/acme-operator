"""Credentials without exposing secrets to the model.

get_credentials hands the model a *reference* ("$secret:<system>.password") instead of
the password. browser_fill resolves the reference at the last moment, so the real value
never enters the model's context, the event log or the report.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agent.models import ToolResult
from agent.tools.base import Tool

if TYPE_CHECKING:
    from agent.tools.base import RunContext

SECRET_PREFIX = "$secret:"


def _credential_env(ctx: "RunContext", system: str) -> dict | None:
    return (ctx.config.systems.get(system) or {}).get("credentials_env")


def resolve_secret(ctx: "RunContext", value: str) -> tuple[str, bool]:
    """Turn '$secret:<system>.<key>' into the real value. Other values pass through unchanged."""
    if not value.startswith(SECRET_PREFIX):
        return value, False
    system, _, key = value[len(SECRET_PREFIX):].partition(".")
    env_name = (_credential_env(ctx, system) or {}).get(key)
    real = os.environ.get(env_name or "", "")
    if not real:
        raise ValueError(f"Unknown or unset secret reference {value}")
    ctx.secrets.add(real)
    return real, True


def credentials_for(ctx: "RunContext", system: str) -> tuple[str, str]:
    """Real username and password, for tools that sign in on the model's behalf."""
    env = _credential_env(ctx, system) or {}
    username, password = os.environ.get(env.get("username", "")), os.environ.get(env.get("password", ""))
    if not username or not password:
        raise ValueError(f"No credentials configured for '{system}'")
    ctx.secrets.add(password)
    return username, password


class GetCredentials(Tool):
    name = "get_credentials"
    description = ("Get sign-in credentials for a system. Returns the username and a password *reference*; "
                   "type the reference into the password field exactly as given (it is resolved securely).")
    parameters = {
        "type": "object",
        "properties": {"system": {"type": "string", "description": "System name from the system directory"}},
        "required": ["system"],
    }

    def run(self, ctx, system: str) -> ToolResult:
        env = _credential_env(ctx, system)
        if env is None:
            with_creds = [s for s in ctx.config.systems if _credential_env(ctx, s)]
            return ToolResult.fail("not_found", f"No credentials configured for '{system}'. Systems with credentials: {with_creds}.")
        username, password = os.environ.get(env.get("username", "")), os.environ.get(env.get("password", ""))
        if not username or not password:
            return ToolResult.fail("fatal", f"Credentials for '{system}' are not set in the environment "
                                            f"({env.get('username')}, {env.get('password')}). Ask a human.")
        ctx.secrets.add(password)
        return ToolResult.success({"username": username, "password": f"{SECRET_PREFIX}{system}.password"})
