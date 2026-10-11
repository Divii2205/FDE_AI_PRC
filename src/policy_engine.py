"""Deterministic procurement-policy engine (data/procurement_policy.md, version 2026.09).

Pure code: no LLM. Produces the *minimum* approvals, risk flags and missing information
that any architecture must honour. The LLM layer may add to these, never remove them.
"""
from __future__ import annotations

import re
from typing import Any

APPROVER_ORDER = ["Manager", "Department Head", "Procurement", "Finance", "CFO", "Security", "Privacy", "Legal"]

RECOMMENDATION_TYPES = {
    "request_clarification": "Request clarification from requester",
    "escalate_for_manual_review": "Escalate for manual review",
    "requires_budget_exception": "Route for Finance budget-exception review",
    "consider_existing_tool": "Review existing tool before purchasing",
    "proceed_to_approval_routing": "Route for approvals",
}
# Code enforces these as floors on the LLM's choice: the LLM may pick an equal or
# higher-priority type, never a lower one (e.g. it cannot "proceed" past missing info).
HARD_FLOOR_PRIORITY = {"request_clarification": 3, "escalate_for_manual_review": 2, "requires_budget_exception": 1}

PII_LEVELS = {"employee_pii", "customer_pii"}
SECURITY_DATA_LEVELS = {"source_code", "confidential_documents", "employee_pii", "customer_pii",
                        "credentials", "secrets"}
LOW_SENSITIVITY_LEVELS = {"none", "public"}
UNKNOWN_VALUES = {"", "unknown", "tbd", "n/a", "na", "none provided", "?"}
SECURITY_INTEGRATION_PATTERN = re.compile(
    r"production|\bprod\b|cloud account|\baws\b|\bgcp\b|azure|\bgit\b|source[ -]?code|secret|credential|password", re.I)

INJECTION_PATTERNS = [
    r"ignore (all|any|the|previous|prior|above)?\s*(procurement |previous |prior )?(rules|instructions|policy|policies|controls)",
    r"disregard (the |all |any )?(rules|instructions|policy|policies)",
    r"(treat|mark|consider) (this|the) (request )?as (already )?(cfo[- ]|pre[- ]|fully )?approved",
    r"\b(cfo|ceo|vp)[- ]approved\b",
    r"approve (it|this|the request|immediately|now)",
    r"bypass|override (the )?(policy|controls|review)|skip (the )?(review|approval)",
    r"system prompt|you are now|act as|developer mode|reveal (your|the) (instructions|secrets|keys?)",
]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().casefold() in UNKNOWN_VALUES:
        return True
    return False


def scan_for_injection(texts: dict[str, Any]) -> list[dict]:
    findings = []
    for field, text in texts.items():
        if not isinstance(text, str):
            continue
        for pattern in INJECTION_PATTERNS:
            m = re.search(pattern, text, re.I)
            if m:
                findings.append({"field": field, "matched_text": m.group(0)})
                break
    return findings


def approval_threshold(cost: float) -> tuple[list[str], str]:
    if cost <= 1_000:
        return ["Manager"], "Up to $1,000"
    if cost <= 10_000:
        return ["Department Head", "Procurement"], "$1,000.01 - $10,000"
    if cost <= 25_000:
        return ["Department Head", "Finance", "Procurement"], "$10,000.01 - $25,000"
    return ["Department Head", "Finance", "CFO", "Procurement"], "Above $25,000"


def evaluate_policy(request: dict, context: dict, catalog: dict, vendor: dict) -> dict:
    approvals: dict[str, list[str]] = {}
    flags: dict[str, str] = {}
    missing: list[str] = []
    evidence: list[dict] = []

    def need(approver: str, reason: str) -> None:
        approvals.setdefault(approver, []).append(reason)

    def flag(name: str, reason: str) -> None:
        flags.setdefault(name, reason)

    # §1 Required information ---------------------------------------------------
    requester = context.get("requester")
    if not requester:
        missing.append("Requester / department (requester not found in employee directory)")
    if _is_missing(request.get("product_name")) or _is_missing(request.get("vendor_name")):
        missing.append("Product / vendor name")
    cost = request.get("annual_cost_usd")
    if cost is None or (isinstance(cost, (int, float)) and cost <= 0):
        missing.append("Annual cost (or a reasonable annual estimate)")
        cost = None
    if request.get("user_count") in (None, 0):
        missing.append("Number of users / licenses (seat count)")
    justification = request.get("business_justification")
    if _is_missing(justification) or len(str(justification).strip()) < 15:
        missing.append("Business purpose")
    data_level = str(request.get("data_access_level") or "").strip().casefold()
    if _is_missing(data_level):
        missing.append("Intended data-access level (what data the tool will touch)")
        data_level = "unknown"
    integrations = request.get("requested_integrations")
    if integrations is None:
        missing.append("Required integrations")
        integrations = []
    if missing:
        flag("missing_information", "Required request fields are missing: " + "; ".join(missing))

    # §2 Budget ------------------------------------------------------------------
    budget_check = context.get("budget_check", {})
    if budget_check.get("status") == "exceeds_budget":
        flag("budget_insufficient", f"Cost exceeds available budget by ${budget_check['shortfall_usd']:,.0f}")
        need("Finance", "Budget exception: cost exceeds department's available software budget (§2)")

    # §3 Overlap -----------------------------------------------------------------
    overlap = [m for m in catalog.get("matches", []) if m["relation"] in
               {"same_product_family_already_licensed", "alternative_in_same_category"}]
    exact_duplicate = any(m["relation"] == "same_product_family_already_licensed" for m in overlap)
    if overlap:
        names = ", ".join(f"{m['product_name']} ({m['relation'].replace('_', ' ')})" for m in overlap)
        flag("existing_tool_overlap", f"Existing approved software may cover this need: {names}")

    # §4 Financial thresholds ------------------------------------------------------
    threshold_band = None
    if cost is not None:
        chain, threshold_band = approval_threshold(float(cost))
        for approver in chain:
            need(approver, f"Financial threshold {threshold_band} (§4)")
    else:
        need("Procurement", "Intake triage - approval chain cannot be set until annual cost is known (§1, §4)")

    # §5 Security ------------------------------------------------------------------
    api_status = vendor.get("vendor_risk_api_status")
    security_reasons = []
    if data_level in SECURITY_DATA_LEVELS:
        security_reasons.append(f"data access level '{data_level}'")
    if data_level.startswith("production"):
        security_reasons.append(f"production data access '{data_level}'")
    risky_integrations = [i for i in integrations if SECURITY_INTEGRATION_PATTERN.search(str(i))]
    if risky_integrations:
        security_reasons.append(f"sensitive integration(s): {', '.join(risky_integrations)}")
    if vendor.get("security_review_expired"):
        security_reasons.append("vendor security review expired")
        flag("vendor_review_expired", f"Latest vendor security review ({vendor.get('latest_review_date')}) is older than 365 days "
                                      f"or reported expired as of {vendor.get('reference_date')}")
    elif not vendor.get("security_assessment_current") and api_status == "ok" and not vendor.get("conflicts"):
        security_reasons.append("vendor security assessment missing or not completed")
    if vendor.get("conflicts"):
        security_reasons.append("registry and vendor-risk service disagree")
        flag("conflicting_vendor_evidence", "; ".join(vendor["conflicts"]))
    if api_status != "ok":
        security_reasons.append("vendor-risk status could not be verified")
        flag("vendor_risk_unavailable", f"Vendor-risk service {api_status}: {vendor.get('vendor_risk_api_error')}")
    if security_reasons:
        need("Security", "Security review required: " + "; ".join(security_reasons) + " (§5)")
        flag("security_review_required", "; ".join(security_reasons))

    # §6 Privacy -------------------------------------------------------------------
    privacy_reasons = []
    if data_level in PII_LEVELS:
        privacy_reasons.append(f"tool will process {data_level.replace('_', ' ')}")
    outside_region = vendor.get("stores_data_outside_region")
    if outside_region and data_level not in LOW_SENSITIVITY_LEVELS:
        privacy_reasons.append("vendor stores data outside the operating region")
    if privacy_reasons:
        need("Privacy", "Privacy review required: " + "; ".join(privacy_reasons) + " (§6)")
        flag("privacy_review_required", "; ".join(privacy_reasons))

    # §7 Legal ---------------------------------------------------------------------
    legal_reasons = []
    if vendor.get("is_new_vendor") and cost is not None and cost >= 10_000:
        legal_reasons.append(f"new vendor with annual spend ${cost:,.0f} (>= $10,000)")
    terms = str(vendor.get("legal_terms_status") or "Unknown")
    if terms.casefold() != "approved":
        legal_reasons.append(f"legal terms status '{terms}' (not approved/standard)")
    if outside_region and (data_level in PII_LEVELS or data_level == "confidential_documents"):
        legal_reasons.append("cross-region processing of sensitive data")
    if legal_reasons:
        need("Legal", "Legal review required: " + "; ".join(legal_reasons) + " (§7)")
        flag("legal_review_required", "; ".join(legal_reasons))

    # §8 AI tools ------------------------------------------------------------------
    is_ai = "ai" in str(request.get("category", "")).casefold().split() or " ai " in f" {request.get('product_name', '')} ".casefold()
    limited = [m for m in catalog.get("matches", []) if "limited" in str(m.get("status", "")).casefold()]
    if is_ai or limited:
        note = "AI tool: prior purchases do not authorise new data classes/use cases (§8)."
        if limited:
            note += " Existing approval is limited-use: " + ", ".join(m["product_name"] for m in limited) + "."
        evidence.append({"source": "evaluate_policy", "finding": note, "reference": "Policy §8"})

    # §9 Prompt injection -------------------------------------------------------------
    texts = {
        "request.business_justification": request.get("business_justification"),
        "request.product_name": request.get("product_name"),
        "vendor_registry.notes": (vendor.get("registry") or {}).get("notes"),
        "vendor_risk_api.notes": (vendor.get("vendor_risk_api") or {}).get("notes"),
    }
    injection = scan_for_injection(texts)
    if injection:
        flag("prompt_injection_detected",
             "Untrusted text attempted to change rules/approvals: " + "; ".join(f"{f['field']}: \"{f['matched_text']}\"" for f in injection))
        evidence.append({"source": "evaluate_policy",
                         "finding": "Embedded instruction(s) in business data ignored: "
                                    + "; ".join(f"{f['field']} contains \"{f['matched_text']}\"" for f in injection),
                         "reference": "Policy §9"})

    # Deterministic recommendation (used as floor for the LLM and as rules-only fallback)
    if missing:
        rec = "request_clarification"
    elif api_status != "ok" or vendor.get("conflicts"):
        rec = "escalate_for_manual_review"
    elif "budget_insufficient" in flags:
        rec = "requires_budget_exception"
    elif exact_duplicate:
        rec = "consider_existing_tool"
    else:
        rec = "proceed_to_approval_routing"

    ordered = [a for a in APPROVER_ORDER if a in approvals]
    evidence.insert(0, {
        "source": "evaluate_policy",
        "finding": (f"Threshold band {threshold_band} -> minimum approvals: {', '.join(ordered)}."
                    if threshold_band else f"Annual cost unknown -> threshold band cannot be determined; approvals so far: {', '.join(ordered)}."),
        "reference": "Policy §4",
    })
    for name in ("Security", "Privacy", "Legal", "Finance"):
        if name in approvals and not (name == "Finance" and all("threshold" in r for r in approvals[name])):
            evidence.append({"source": "evaluate_policy", "finding": approvals[name][-1], "reference": "Policy"})

    return {
        "missing_information": missing,
        "required_approvals": ordered,
        "approval_reasons": {a: approvals[a] for a in ordered},
        "risk_flags": list(flags),
        "risk_flag_reasons": flags,
        "threshold_band": threshold_band,
        "data_access_level": data_level,
        "prompt_injection_findings": injection,
        "deterministic_recommendation": rec,
        "hard_floor": rec if rec in HARD_FLOOR_PRIORITY else None,
        "human_review_required": True,
        "evidence": evidence,
    }
