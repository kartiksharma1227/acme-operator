"""The tool that lets the operator involve a person, through whichever HumanChannel is plugged in.

There is deliberately no "ask for approval" tool: approvals come only from the policy gate,
at the moment of submission, based on the values actually on the page. A second, voluntary
approval route made the model ask twice for the same thing.
"""
from __future__ import annotations

import re

from agent.models import ToolResult
from agent.tools.base import Tool


class AskHuman(Tool):
    name = "ask_human"
    description = ("Ask a person for information you cannot find yourself, or for a judgment call the "
                   "procedure says needs a human. Batch related questions into one. Don't ask about things "
                   "you can look up, and don't use this to ask for policy approvals (those happen automatically).")
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "Optional suggested answers"},
        },
        "required": ["question"],
    }
    read_only = False  # the verifier works without a human

    def run(self, ctx, question: str, options: list[str] | None = None) -> ToolResult:
        if re.search(r"\bapprov", question, re.IGNORECASE):
            # One approval route only: the gate asks at submit time, with the real values on the page.
            return ToolResult.fail("validation", "Don't ask for approvals here. Submit the action: if company policy "
                                                 "requires approval, the system pauses and asks a human automatically, "
                                                 "and tells you if it was denied.")
        ctx.emit("question_asked", {"question": question, "options": options or []})
        response = ctx.human.ask("question", {"question": question, "options": options or []})
        ctx.emit("question_answered", {"question": question, "answer": response.answer})
        ctx.state.bump("human_questions")
        return ToolResult.success({"answer": response.answer})


HUMAN_TOOLS = [AskHuman]
