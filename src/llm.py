"""Provider-neutral LLM layer.

Agents talk to a `Conversation` (add_user / step / add_tool_results). Two adapters:
  * AnthropicConversation   - Claude via the Anthropic SDK, JSON enforced with output_config.
  * OpenAICompatConversation - any OpenAI-compatible REST endpoint: Google Gemini (free tier), Groq,
                               OpenRouter ':free' models, local Ollama.
Provider and model are chosen in src/config.py from environment variables.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.config import LLM_EFFORT, PROVIDERS, model_name, provider


class LLMError(RuntimeError):
    """Raised when the model cannot produce a usable response; callers fall back to rules-only."""


@dataclass
class LLMUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    stages: list[str] = field(default_factory=list)

    def record(self, stage: str, input_tokens: int | None, output_tokens: int | None) -> None:
        self.calls += 1
        self.stages.append(stage)
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    tool_calls: list[ToolCall]
    output: dict | None = None


# ----------------------------------------------------------------------------- helpers
def extract_json(text: str) -> dict | None:
    """Parse a JSON object from model text (tolerates ```json fences / surrounding prose)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    if "{" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for c in candidates:
        try:
            data = json.loads(c)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def conform(data: dict, schema: dict) -> dict:
    """Fill missing required keys with empty values so weaker models can't crash the pipeline.
    Semantic validation (enums, floors) happens later in the guardrail merge."""
    empty = {"array": [], "string": "", "boolean": False, "object": {}}
    out = dict(data)
    for key in schema.get("required", []):
        if key not in out or out[key] is None:
            out[key] = empty.get(schema["properties"][key].get("type"), None)
        elif schema["properties"][key].get("type") == "array" and not isinstance(out[key], list):
            out[key] = [out[key]]
    return out


class Conversation:
    def __init__(self, *, stage: str, usage: LLMUsage, system: str, schema: dict, tools: list[dict] | None = None):
        self.stage, self.usage, self.system, self.schema, self.tools = stage, usage, system, schema, tools or []

    def add_user(self, text: str) -> None: ...
    def step(self) -> Turn: ...
    def add_tool_results(self, results: list[tuple[ToolCall, str]]) -> None: ...


# ----------------------------------------------------------------------------- Anthropic
# Server-side refusal fallback is supported on these models (Claude API).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_anthropic_client = None


def anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(max_retries=2, timeout=120)
    return _anthropic_client


class AnthropicConversation(Conversation):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.messages: list[dict] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": content} for call, content in results]})

    def step(self) -> Turn:
        model = model_name()
        kwargs: dict[str, Any] = dict(
            model=model, max_tokens=16000, system=self.system, messages=self.messages,
            output_config={"effort": LLM_EFFORT, "format": {"type": "json_schema", "schema": self.schema}},
        )
        if self.tools:
            kwargs["tools"] = self.tools
        try:
            if model in _FALLBACK_MODELS:
                response = anthropic_client().beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                response = anthropic_client().messages.create(**kwargs)
        except ImportError as exc:  # e.g. Windows Application Control blocking a compiled dependency
            raise LLMError(f"Anthropic SDK could not be loaded: {exc}") from exc
        except Exception as exc:
            # Classify SDK errors by name so the SDK is only imported when Claude is actually used.
            kind = type(exc).__name__
            if kind == "APIConnectionError" or kind == "APITimeoutError":
                raise LLMError(f"Could not reach the Claude API: {exc}") from exc
            if kind == "RateLimitError":
                raise LLMError("Claude API rate limit reached") from exc
            if hasattr(exc, "status_code"):
                raise LLMError(f"Claude API error {exc.status_code}: {getattr(exc, 'message', exc)}") from exc
            raise

        self.usage.record(self.stage, getattr(response.usage, "input_tokens", 0), getattr(response.usage, "output_tokens", 0))
        if response.stop_reason == "refusal":
            raise LLMError("Model declined the request (refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMError("Model output truncated (max_tokens)")
        self.messages.append({"role": "assistant", "content": response.content})

        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in response.content if getattr(b, "type", None) == "tool_use"]
        if calls:
            return Turn(calls)
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        data = extract_json(text)
        if data is None:
            raise LLMError(f"Model returned invalid JSON: {text[:200]!r}")
        return Turn([], conform(data, self.schema))


# ----------------------------------------------------------------------------- OpenAI-compatible
# Plain HTTPS via `requests` (pure Python) instead of the `openai` SDK: the SDK imports the compiled
# `jiter` module, which Windows Smart App Control / Application Control can block from loading.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _retry_delay_seconds(response) -> float | None:
    """Seconds the provider asks us to wait: Retry-After header or Google's RetryInfo ("24936s")."""
    header = response.headers.get("retry-after")
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', getattr(response, "text", "") or "")
    return float(match.group(1)) if match else None


def post_chat_completion(name: str, payload: dict, max_retries: int = 5) -> dict:
    """POST {base_url}/chat/completions with retry/backoff on rate limits and transient errors."""
    import os
    import time

    import requests

    cfg = PROVIDERS[name]
    base = (os.getenv("LLM_BASE_URL") or cfg["base_url"]).rstrip("/")
    key = next((os.getenv(k) for k in cfg["env_keys"] if os.getenv(k)), None)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(f"{base}/chat/completions", json=payload, headers=headers, timeout=120)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise LLMError(f"Could not reach {name}: {type(exc).__name__}") from exc
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            wait = _retry_delay_seconds(r)
            if wait is not None and wait > 60:
                # Daily free-tier quota (e.g. Gemini: 20 requests/day/model) - retrying now is pointless.
                raise LLMError(f"{name} daily free-tier quota reached for model '{payload.get('model')}' "
                               f"(resets in ~{wait / 3600:.1f} h). Try another MODEL_NAME or wait.")
        if r.status_code in _RETRY_STATUS and attempt < max_retries:
            retry_after = r.headers.get("retry-after")
            time.sleep(float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit()
                       else min(2 ** attempt * 2, 30))
            continue
        if r.status_code == 429:
            raise LLMError(f"{name} rate limit / free-tier quota reached")
        if r.status_code >= 400:
            raise LLMError(f"{name} API error {r.status_code}: {r.text[:300]}")
        return r.json()
    raise LLMError(f"{name}: retries exhausted")


class OpenAICompatConversation(Conversation):
    def __init__(self, **kw):
        super().__init__(**kw)
        instruction = (
            "\n\nOUTPUT FORMAT: when you have finished calling tools, reply with ONLY one JSON object (no prose, "
            f"no markdown) that matches this JSON Schema:\n{json.dumps(self.schema)}"
        )
        self.messages: list[dict] = [{"role": "system", "content": self.system + instruction}]
        self.oa_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": t["input_schema"]}} for t in self.tools]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results):
        for call, content in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    def _create(self, **extra) -> dict:
        name = provider()
        payload: dict[str, Any] = dict(model=model_name(), messages=self.messages, temperature=0, max_tokens=4096)
        payload.update(extra)
        data = post_chat_completion(name, payload)
        usage = data.get("usage") or {}
        self.usage.record(self.stage, usage.get("prompt_tokens"), usage.get("completion_tokens"))
        if not data.get("choices"):
            raise LLMError(f"{name} returned no choices: {str(data)[:200]}")
        return data["choices"][0].get("message") or {}

    def step(self) -> Turn:
        msg = self._create(**({"tools": self.oa_tools, "tool_choice": "auto"} if self.oa_tools else {}))
        content = msg.get("content") or ""
        if msg.get("tool_calls"):
            calls = []
            for tc in msg["tool_calls"]:
                tc.setdefault("type", "function")
                if not tc.get("id"):
                    tc["id"] = f"call_{uuid.uuid4().hex[:8]}"
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append(ToolCall(tc["id"], fn.get("name", ""), args if isinstance(args, dict) else {}))
            # Echo the provider's assistant message back verbatim: Gemini 3 attaches a per-call
            # thought_signature (extra_content) that must be returned unchanged on the next request.
            self.messages.append({**msg, "role": "assistant"})
            return Turn(calls)

        self.messages.append({"role": "assistant", "content": content})
        data = extract_json(content)
        if data is None:
            # One repair attempt in JSON mode (no tools: some providers reject tools + JSON mode together).
            self.messages.append({"role": "user", "content": "Reply with ONLY the final JSON object matching the schema."})
            repaired = self._create(response_format={"type": "json_object"}).get("content") or ""
            data = extract_json(repaired)
            if data is None:
                raise LLMError(f"Model did not return valid JSON: {repaired[:200]!r}")
        return Turn([], conform(data, self.schema))


def conversation(**kw) -> Conversation:
    return AnthropicConversation(**kw) if provider() == "anthropic" else OpenAICompatConversation(**kw)
