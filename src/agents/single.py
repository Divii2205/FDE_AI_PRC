"""Architecture A - single agent.

One Claude agent with four tools runs the whole review in a tool-use loop and returns the
structured decision. Code then merges it with the deterministic policy result.
"""
from __future__ import annotations

from src import llm
from src.agents.common import DECISION_SCHEMA, JUDGMENT_GUIDE, tool_result_text
from src.config import MAX_AGENT_TURNS
from src.tools import TOOL_DEFINITIONS, Toolbox

SYSTEM = f"""\
You are the Procurement Request Copilot. You review one software/service purchase request for the
procurement team, gather evidence with tools, and recommend the next action for a human reviewer.

Process:
1. Call get_request_context for the request.
2. Call search_software_catalog and check_vendor_risk using the request's category, vendor and product
   (call them in parallel).
3. Call evaluate_policy.
4. Return the final decision JSON.

{JUDGMENT_GUIDE}"""


def run(toolbox: Toolbox, usage: llm.LLMUsage) -> tuple[dict, list[str]]:
    """Returns (llm_output, guardrail_overrides)."""
    request_id = toolbox.request["request_id"]
    conv = llm.conversation(stage="single_agent", usage=usage, system=SYSTEM, schema=DECISION_SCHEMA,
                            tools=TOOL_DEFINITIONS)
    conv.add_user(f"Review purchase request {request_id}.")
    overrides: list[str] = []
    nudged = False

    for _ in range(MAX_AGENT_TURNS):
        turn = conv.step()
        if turn.tool_calls:
            conv.add_tool_results([(c, tool_result_text(toolbox.call(c.name, c.args))) for c in turn.tool_calls])
            continue

        if "evaluate_policy" not in toolbox.telemetry.tool_names and not nudged:
            # Guardrail: the decision must be grounded in the policy engine. One nudge, then enforce in code.
            nudged = True
            overrides.append("agent answered before calling evaluate_policy; asked to call it")
            conv.add_user("Call evaluate_policy before giving the final decision.")
            continue
        if "evaluate_policy" not in toolbox.telemetry.tool_names:
            overrides.append("agent never called evaluate_policy; orchestrator ran it")
        return turn.output, overrides

    raise llm.LLMError(f"Agent did not finish within {MAX_AGENT_TURNS} turns")
