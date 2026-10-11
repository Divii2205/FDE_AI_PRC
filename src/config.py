from __future__ import annotations

import os
import re
from datetime import date
from functools import lru_cache

from src.data_access import load_policy_text

_DEFAULT_REFERENCE_DATE = date(2026, 9, 30)


@lru_cache(maxsize=1)
def reference_date() -> date:
    """Policy snapshot date used for all date-based checks (never the machine clock)."""
    match = re.search(r"reference date:\*\*\s*(\d{4}-\d{2}-\d{2})", load_policy_text())
    return date.fromisoformat(match.group(1)) if match else _DEFAULT_REFERENCE_DATE


SECURITY_REVIEW_VALID_DAYS = 365

# ---------------------------------------------------------------------------- LLM providers
# Free options: Google Gemini (AI Studio free tier), Groq (free tier), OpenRouter ':free' models,
# Ollama (local, no key). Model names change over time - override with MODEL_NAME.
PROVIDERS: dict[str, dict] = {
    "anthropic": {"env_keys": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"], "base_url": None,
                  "default_model": "claude-opus-5-5"},
    "gemini": {"env_keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "default_model": "gemini-3.8-flash"},
    "groq": {"env_keys": ["GROQ_API_KEY"], "base_url": "https://api.groq.com/openai/v1",
             "default_model": "llama-3.3-70b-versatile"},
    "openrouter": {"env_keys": ["OPENROUTER_API_KEY"], "base_url": "https://openrouter.ai/api/v1",
                   "default_model": "meta-llama/llama-3.3-70b-instruct:free"},
    "ollama": {"env_keys": [], "base_url": "http://localhost:11434/v1", "default_model": "qwen2.5:7b"},
}

LLM_EFFORT = os.getenv("LLM_EFFORT") or "medium"  # Claude only
MAX_AGENT_TURNS = int(os.getenv("MAX_AGENT_TURNS") or 8)


def provider() -> str | None:
    """LLM_PROVIDER if set, otherwise the first provider whose API key is present. None = no LLM."""
    explicit = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if explicit in PROVIDERS:
        return explicit
    for name, cfg in PROVIDERS.items():
        if any(os.getenv(k) for k in cfg["env_keys"]):
            return name
    return None


def model_name() -> str | None:
    name = provider()
    return (os.getenv("MODEL_NAME") or PROVIDERS[name]["default_model"]) if name else None


def llm_mode() -> str:
    """'llm' when a provider is configured, otherwise 'rules_only' (deterministic fallback).

    LLM_MODE=rules_only forces the deterministic path (offline demos/tests).
    """
    if (os.getenv("LLM_MODE") or "").strip().lower() in {"rules_only", "offline"}:
        return "rules_only"
    return "llm" if provider() else "rules_only"
