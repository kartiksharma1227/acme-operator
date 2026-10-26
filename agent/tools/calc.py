"""Exact arithmetic for the operator.

Models are unreliable at mental arithmetic and threshold comparisons, which are exactly
the decisions procedures hinge on. The operator uses this instead, like a person with a
calculator. Only arithmetic is evaluated (a whitelist of syntax), never arbitrary code.
"""
from __future__ import annotations

import ast
import operator

from agent.models import ToolResult
from agent.tools.base import Tool

BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
CMP_OPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
           ast.Eq: operator.eq, ast.NotEq: operator.ne}
FUNCS = {"abs": abs, "round": round, "min": min, "max": max}


def evaluate(expression: str):
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in BIN_OPS:
            return BIN_OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return -ev(node.operand) if isinstance(node.op, ast.USub) else ev(node.operand)
        if isinstance(node, ast.Compare) and all(type(op) in CMP_OPS for op in node.ops):
            left = ev(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = ev(comparator)
                if not CMP_OPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            values = [ev(v) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS:
            return FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError(f"unsupported syntax: {ast.dump(node)[:60]}")

    cleaned = expression.replace("$", "").replace(",", "")
    result = ev(ast.parse(cleaned, mode="eval"))
    return round(result, 10) if isinstance(result, float) else result


class Calculate(Tool):
    name = "calculate"
    description = ("Evaluate arithmetic exactly. Use it for every difference, total, tolerance or threshold "
                   "check instead of doing math in your head, e.g. '3975.00 - 3500.00' or "
                   "'abs(1203.50 - 1200.00) <= 5'. Supports + - * / % ** ( ), comparisons, and/or, abs, round, min, max. "
                   "'$' and thousands separators are ignored.")
    parameters = {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}

    def run(self, ctx, expression: str) -> ToolResult:
        try:
            return ToolResult.success({"expression": expression, "result": evaluate(expression)})
        except (ValueError, SyntaxError, TypeError, ZeroDivisionError) as exc:
            return ToolResult.fail("validation", f"Could not evaluate '{expression}': {exc}")
