"""Orchestrates one review: tools + agent architecture + deterministic guardrails."""
from __future__ import annotations

import time

from src import llm
from src.agents import single, staged
from src.agents.common import finalize
from src.config import llm_mode
from src.contracts import Architecture, ProcurementDecision
from src.telemetry import RunTelemetryCounter
from src.tools import Toolbox

ARCHITECTURES = {"single": single.run, "staged": staged.run}


def analyze(request: dict, architecture: Architecture = "single") -> ProcurementDecision:
    if architecture not in ARCHITECTURES:
        raise ValueError(f"Unknown architecture {architecture!r}; expected one of {list(ARCHITECTURES)}")
    start = time.perf_counter()
    telemetry = RunTelemetryCounter()
    toolbox = Toolbox(request, telemetry)
    usage = llm.LLMUsage()
    mode = llm_mode()
    llm_output, overrides = None, []

    if mode == "llm":
        try:
            llm_output, overrides = ARCHITECTURES[architecture](toolbox, usage)
        except llm.LLMError as exc:
            # Degrade safely: deterministic decision, clearly labelled, still routed to humans.
            mode = "rules_only_fallback"
            overrides.append(f"LLM unavailable ({exc}); returned deterministic rules-only decision")

    # The policy engine result is authoritative; make sure it exists even if the agent skipped it.
    policy = toolbox.results.get("evaluate_policy") or toolbox.call("evaluate_policy")

    decision = finalize(toolbox, policy, llm_output, mode=mode, architecture=architecture, telemetry=telemetry,
                        llm_usage=usage, overrides=overrides)
    decision.telemetry.latency_ms = round((time.perf_counter() - start) * 1000, 1)
    if mode == "rules_only_fallback":
        decision.risk_flags.append("llm_unavailable")
    return decision
