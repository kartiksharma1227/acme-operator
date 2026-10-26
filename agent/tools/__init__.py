"""The operator's toolbox. Adding a capability means adding a Tool class to this list."""
from agent.tools.base import RunContext, Tool, ToolRegistry
from agent.tools.browser import BROWSER_TOOLS
from agent.tools.calc import Calculate
from agent.tools.credentials import GetCredentials
from agent.tools.files import FILE_TOOLS
from agent.tools.handbook import HANDBOOK_TOOLS
from agent.tools.human_tools import HUMAN_TOOLS
from agent.tools.plan_tools import PLAN_TOOLS

ALL_TOOLS: list[type[Tool]] = [
    *HANDBOOK_TOOLS, *FILE_TOOLS, GetCredentials, *BROWSER_TOOLS, Calculate, *HUMAN_TOOLS, *PLAN_TOOLS,
]


def build_registry() -> ToolRegistry:
    return ToolRegistry([tool() for tool in ALL_TOOLS])


__all__ = ["RunContext", "Tool", "ToolRegistry", "build_registry", "ALL_TOOLS"]
