from __future__ import annotations

from src.contracts import Architecture, ProcurementDecision
from src.data_access import get_request
from src.pipeline import analyze


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter.

    Keep this function callable by the public/hidden evaluation harness.
    Your internal implementation may use any framework, modules, agents, tools,
    deterministic checks, or orchestration strategy.

    architecture="single": one Claude agent with four tools (Architecture A).
    architecture="staged": Analyst agent -> deterministic policy engine -> Reviewer agent (Architecture B).
    Without Claude credentials (or LLM_MODE=rules_only) both run the deterministic rules-only path.
    """
    return analyze(get_request(request_id), architecture)
