"""The only place that talks to a model provider.

The rest of the runtime keeps the conversation in one neutral shape (chat-style messages)
and asks this adapter for "the next turn" or "a structured object". Swapping provider or
API means changing this file only. Two OpenAI backends are supported:

  responses (default): the Responses API, which allows reasoning *and* tool use together
                       on current models.
  chat:                Chat Completions. Some models reject reasoning_effort alongside tools
                       there, so it falls back to 'none' / no setting and remembers that.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from openai import BadRequestError, OpenAI
from pydantic import BaseModel

from agent.models import RunState

T = TypeVar("T", bound=BaseModel)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    assistant_message: dict  # chat-style, ready to append to the conversation


def to_responses_input(messages: list[dict]) -> list[dict]:
    """Chat-style messages -> Responses API input items."""
    items: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
        elif m["role"] == "assistant":
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
            for call in m.get("tool_calls") or []:
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
        else:
            items.append({"role": m["role"], "content": m["content"]})
    return items


def to_responses_tools(tools: list[dict]) -> list[dict]:
    # Non-strict: our schemas have optional parameters, which strict mode would reject.
    return [{"type": "function", "name": t["function"]["name"], "description": t["function"]["description"],
             "parameters": t["function"]["parameters"], "strict": False} for t in tools]


class LLM:
    def __init__(self, model: str, state: RunState, emit: Callable[[str, dict], None],
                 reasoning_effort: str | None = None, client: OpenAI | None = None, api: str | None = None):
        self.model = model
        self.state = state
        self.emit = emit
        self.reasoning_effort = reasoning_effort or None
        self._tools_effort = self.reasoning_effort  # chat API only: may be lowered if rejected with tools
        self.api = api or os.environ.get("LLM_API") or "responses"
        # The SDK already retries rate limits, 5xx and timeouts with exponential backoff.
        # LLM_BASE_URL / LLM_API_KEY point at any OpenAI-compatible endpoint (e.g. a local proxy)
        # without touching OPENAI_API_KEY; unset, the official API is used.
        self.client = client or OpenAI(max_retries=4, timeout=180, base_url=os.environ.get("LLM_BASE_URL") or None,
                                       api_key=os.environ.get("LLM_API_KEY") or None)
        self.price_in = float(os.environ.get("PRICE_INPUT_PER_M") or 0)
        self.price_out = float(os.environ.get("PRICE_OUTPUT_PER_M") or 0)

    @property
    def has_prices(self) -> bool:
        return bool(self.price_in or self.price_out)

    # ---------- public ----------

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        started = time.monotonic()
        response = self._responses_turn(messages, tools) if self.api == "responses" else self._chat_turn(messages, tools)
        self._account(response, started, "chat", messages)
        return self._parse_turn(response)

    def structured(self, messages: list[dict], model_cls: type[T]) -> T:
        started = time.monotonic()
        if self.api == "responses":
            response = self.client.responses.parse(model=self.model, input=to_responses_input(messages),
                                                   text_format=model_cls, store=False, **self._reasoning())
            parsed = response.output_parsed
        else:
            response = self.client.chat.completions.parse(model=self.model, messages=messages,
                                                          response_format=model_cls, **self._effort())
            parsed = response.choices[0].message.parsed
        self._account(response, started, f"structured:{model_cls.__name__}", messages)
        if parsed is None:
            raise RuntimeError(f"The model returned no {model_cls.__name__}.")
        return parsed

    # ---------- backends ----------

    def _reasoning(self) -> dict:
        return {"reasoning": {"effort": self.reasoning_effort}} if self.reasoning_effort else {}

    def _effort(self) -> dict:
        # No temperature anywhere: GPT-5-family models only accept the default.
        return {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}

    def _responses_turn(self, messages: list[dict], tools: list[dict] | None):
        kwargs = {"model": self.model, "input": to_responses_input(messages), "store": False, **self._reasoning()}
        if tools:
            kwargs["tools"] = to_responses_tools(tools)
        return self.client.responses.create(**kwargs)

    def _chat_turn(self, messages: list[dict], tools: list[dict] | None):
        if not tools:
            return self.client.chat.completions.create(model=self.model, messages=messages, **self._effort())
        kwargs = {"model": self.model, "messages": messages, "tools": tools}
        if self._tools_effort:
            kwargs["reasoning_effort"] = self._tools_effort
        while True:
            try:
                return self.client.chat.completions.create(**kwargs)
            except BadRequestError as exc:
                current = kwargs.get("reasoning_effort")
                if "reasoning_effort" not in str(exc) or current is None:
                    raise
                fallback = "none" if current != "none" else None
                self.emit("llm_fallback", {"model": self.model, "reasoning_effort": current, "now": fallback,
                                           "reason": str(exc)[:200]})
                self._tools_effort = fallback
                kwargs.pop("reasoning_effort")
                if fallback:
                    kwargs["reasoning_effort"] = fallback

    def _parse_turn(self, response) -> LLMResponse:
        if self.api == "responses":
            calls = [ToolCall(item.call_id, item.name, item.arguments or "{}")
                     for item in response.output if item.type == "function_call"]
            content = response.output_text or None
        else:
            message = response.choices[0].message
            calls = [ToolCall(c.id, c.function.name, c.function.arguments or "{}") for c in (message.tool_calls or [])]
            content = message.content
        assistant: dict = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls
            ]
        return LLMResponse(content, calls, assistant)

    def _account(self, response, started: float, kind: str, messages: list[dict]) -> None:
        usage = response.usage
        if self.api == "responses":
            tokens_in, tokens_out = (usage.input_tokens, usage.output_tokens) if usage else (0, 0)
        else:
            tokens_in, tokens_out = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        cost = (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000
        self.state.llm_calls += 1
        self.state.tokens_in += tokens_in
        self.state.tokens_out += tokens_out
        self.state.cost_usd += cost
        event = {"model": self.model, "api": self.api, "kind": kind, "tokens_in": tokens_in,
                 "tokens_out": tokens_out, "latency_s": round(time.monotonic() - started, 2)}
        if self.has_prices:
            event["cost_usd"] = round(cost, 5)
        if os.environ.get("DEBUG_LLM") == "1":
            event["messages"] = messages
        self.emit("llm_call", event)
