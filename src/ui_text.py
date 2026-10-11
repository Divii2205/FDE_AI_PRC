"""Plain-English labels for the Streamlit UI (display only - no business logic here)."""
from __future__ import annotations

# recommendation_type -> (streamlit alert kind, plain headline)
RECOMMENDATION_HEADLINES: dict[str, tuple[str, str]] = {
    "proceed_to_approval_routing": ("success", "Looks fine — send it to the people who must approve"),
    "consider_existing_tool": ("warning", "We may already have a tool for this — check first"),
    "requires_budget_exception": ("warning", "Not enough budget — Finance must decide"),
    "escalate_for_manual_review": ("error", "Needs a closer look by a person"),
    "request_clarification": ("error", "Ask the requester for more details"),
}

APPROVER_EXPLANATIONS: dict[str, str] = {
    "Manager": "the requester's boss",
    "Department Head": "the head of the team",
    "Procurement": "the buying team",
    "Finance": "the money team",
    "CFO": "the top finance boss",
    "Security": "checks the software is safe",
    "Privacy": "protects personal data",
    "Legal": "checks the contract",
}

RISK_FLAG_TEXT: dict[str, str] = {
    "existing_tool_overlap": "We may already own a similar tool",
    "budget_insufficient": "The team does not have enough budget left",
    "security_review_required": "The security team must check it",
    "privacy_review_required": "Personal data is involved — privacy check needed",
    "legal_review_required": "The contract must be checked by Legal",
    "vendor_review_expired": "The vendor's safety check is out of date",
    "conflicting_vendor_evidence": "Our records about this vendor don't match",
    "vendor_risk_unavailable": "We couldn't reach the vendor safety service",
    "prompt_injection_detected": "The request contained text trying to trick the system (ignored)",
    "missing_information": "Some required details are missing",
    "llm_unavailable": "The AI was not available — rules-only answer",
}

DATA_ACCESS_TEXT: dict[str, str] = {
    "none": "No company data",
    "internal_documents": "Internal documents",
    "internal_marketing": "Marketing material",
    "confidential_documents": "Confidential documents",
    "customer_pii": "Customers' personal data",
    "employee_pii": "Employees' personal data",
    "source_code": "Source code",
    "production_telemetry": "Live system data",
    "unknown": "Not given",
}

SOURCE_TEXT: dict[str, str] = {
    "get_request_context": "Request & requester",
    "budget_check": "Budget check",
    "search_software_catalog": "Software we already own",
    "purchase_history": "Past purchases",
    "vendor_registry": "Vendor list",
    "vendor_risk_api": "Vendor safety service",
    "vendor_consistency_check": "Vendor records check",
    "evaluate_policy": "Company rules check",
    "copilot_analysis": "AI summary",
}

URGENCY_TEXT: dict[str, str] = {"normal": "Normal", "high": "High", "urgent": "Urgent"}

# radio label -> architecture value
VIEW_OPTIONS: dict[str, str] = {
    "One AI helper (simple)": "single",
    "Two AI helpers (step by step)": "staged",
    "Compare both side by side": "compare",
}
VIEW_HELP = (
    "**One AI helper:** one AI looks at all the facts and gives advice in one go.  \n"
    "**Two AI helpers:** the first AI gathers the facts, the second AI writes the advice.  \n"
    "**Compare both:** runs both ways so you can see the difference."
)

ARCH_TEXT = {"single": "One AI helper", "staged": "Two AI helpers"}

REVIEW_ACTIONS = [
    "Send to the approvers",
    "Send back to the requester for more details",
    "Ask Security / a specialist to look closer",
    "Say no to this request",
]

GLOSSARY = [
    ("Approval", "A person saying \"yes, you may buy this\". Bigger or riskier purchases need more people to say yes."),
    ("Vendor", "The company that sells the software."),
    ("Budget", "The money a team is allowed to spend this year."),
    ("Overlap", "When the company already pays for a tool that does the same job."),
    ("Prompt injection", "Text hidden in a request that tries to trick the AI into doing something else. The tool ignores it."),
    ("Human review", "A person reads the advice and makes the real decision. The copilot never decides alone."),
]


def humanize(code: object) -> str:
    """'some_snake_code' -> 'Some snake code'."""
    text = str(code or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Not given"


def data_access_label(code: object) -> str:
    if not code:
        return "Not given"
    return DATA_ACCESS_TEXT.get(str(code), humanize(code))


def source_label(code: object) -> str:
    return SOURCE_TEXT.get(str(code), humanize(code))
