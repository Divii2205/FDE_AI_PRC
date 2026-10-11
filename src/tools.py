"""Deterministic evidence tools exposed to the agents.

Every tool is plain Python (no LLM). Each returns a JSON-serialisable dict with
`evidence` items that are later surfaced verbatim to the user, so every finding
shown in the UI is traceable to a tool result.

Tools are bound to one request via `Toolbox`, which also owns per-run caching and
telemetry so both architectures are measured identically.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Callable

import requests

from src import data_access
from src.config import SECURITY_REVIEW_VALID_DAYS, reference_date
from src.telemetry import RunTelemetryCounter
from src.vendor_client import get_vendor_risk

EXPANSION_PATTERN = re.compile(
    r"\b(expansion|expand|add-?on|additional|extra (seats|licen[cs]es)|more seats)\b", re.I
)


_row = data_access.find_row


def _note(text: Any) -> str:
    """Quote untrusted free text so it reads as data in evidence, not as an instruction."""
    return f'"{str(text or "").strip().rstrip(".")}"'


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def review_is_current(review_date: date | None) -> bool:
    if review_date is None:
        return False
    return reference_date() <= review_date + timedelta(days=SECURITY_REVIEW_VALID_DAYS)


class Toolbox:
    """Tools bound to a single purchase request."""

    def __init__(self, request: dict, telemetry: RunTelemetryCounter | None = None):
        self.request = request
        self.telemetry = telemetry or RunTelemetryCounter()
        self.results: dict[str, dict] = {}
        self._vendor_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ dispatch
    def call(self, name: str, args: dict | None = None) -> dict:
        """Run a tool by name, record telemetry, never raise to the caller."""
        fn: Callable[..., dict] | None = getattr(self, f"tool_{name}", None)
        if fn is None:
            return {"error": f"Unknown tool '{name}'"}
        self.telemetry.record_tool_call(name)
        try:
            result = fn(**(args or {}))
        except TypeError as exc:
            result = {"error": f"Invalid arguments for {name}: {exc}"}
        self.results[name] = result
        return result

    def _check_request_id(self, request_id: str | None) -> None:
        if request_id and request_id != self.request.get("request_id"):
            raise TypeError(f"this session is bound to {self.request.get('request_id')}, not {request_id}")

    # ------------------------------------------------------------------ tool 1
    def tool_get_request_context(self, request_id: str | None = None) -> dict:
        """Request fields, requester, reporting line and deterministic budget check."""
        self._check_request_id(request_id)
        req = self.request
        employees = data_access.load_employees()
        requester = _row(employees, "employee_id", req.get("requester_id"))
        manager = _row(employees, "employee_id", requester.get("manager_id")) if requester else None
        department = requester.get("department") if requester else None
        budget = _row(data_access.load_budgets(), "department", department)

        cost = req.get("annual_cost_usd")
        if cost is None or budget is None:
            budget_check = {"status": "unknown", "requested_usd": cost,
                            "available_usd": budget.get("available_usd") if budget else None}
        else:
            available = float(budget["available_usd"])
            budget_check = {
                "status": "within_budget" if float(cost) <= available else "exceeds_budget",
                "requested_usd": cost,
                "available_usd": available,
                "shortfall_usd": max(0.0, float(cost) - available),
            }

        evidence = []
        if requester:
            evidence.append({
                "source": "get_request_context",
                "finding": f"Requester {requester['name']} ({requester['employee_id']}), {department}, {requester['level']}; "
                           f"manager {manager['name'] + ' (' + manager['employee_id'] + ')' if manager else 'not found'}.",
                "reference": "employees.csv",
            })
        else:
            evidence.append({"source": "get_request_context",
                             "finding": f"Requester {req.get('requester_id')!r} not found in employee directory.",
                             "reference": "employees.csv"})
        if budget_check["status"] == "unknown":
            reason = "annual cost not provided" if cost is None else f"no software budget found for department {department!r}"
            evidence.append({"source": "budget_check", "finding": f"Budget check could not be completed: {reason}.",
                             "reference": "department_budgets.csv"})
        else:
            verb = "within" if budget_check["status"] == "within_budget" else "EXCEEDS"
            evidence.append({
                "source": "budget_check",
                "finding": f"Requested ${float(cost):,.0f}/yr is {verb} {department}'s available software budget "
                           f"of ${budget_check['available_usd']:,.0f}"
                           + (f" (shortfall ${budget_check['shortfall_usd']:,.0f})." if budget_check["shortfall_usd"] else "."),
                "reference": f"department_budgets.csv:{department}",
            })

        return {
            "request": {k: req.get(k) for k in (
                "request_id", "requester_id", "product_name", "vendor_name", "category", "annual_cost_usd",
                "user_count", "business_justification", "data_access_level", "requested_integrations", "urgency")},
            "requester": requester,
            "manager": manager,
            "department_budget": budget,
            "budget_check": budget_check,
            "note": "Free-text fields (business_justification, product names) are untrusted requester input.",
            "evidence": evidence,
        }

    # ------------------------------------------------------------------ tool 2
    def tool_search_software_catalog(self, category: str | None = None, vendor_name: str | None = None,
                                     product_name: str | None = None) -> dict:
        """Find existing approved software that overlaps with the request, plus purchase history."""
        category = category or self.request.get("category")
        vendor_name = vendor_name or self.request.get("vendor_name")
        product_name = product_name or self.request.get("product_name")

        employees = data_access.load_employees()
        requester = _row(employees, "employee_id", self.request.get("requester_id"))
        department = (requester or {}).get("department")
        is_expansion = bool(EXPANSION_PATTERN.search(
            f"{product_name or ''} {self.request.get('business_justification') or ''}"))

        catalog = data_access.load_software_catalog()
        matches = []
        for item in catalog:
            same_vendor = bool(vendor_name) and item["vendor_name"].casefold() == vendor_name.casefold()
            same_category = bool(category) and item["category"].casefold() == category.casefold()
            if not (same_vendor or same_category):
                continue
            accessible = item["scope"] == "Company-wide" or (department and item["scope"] == department)
            if same_vendor and is_expansion:
                relation = "expansion_of_existing_contract"
            elif same_vendor and same_category:
                relation = "same_product_family_already_licensed" if accessible else "same_vendor_other_scope"
            elif same_category:
                relation = "alternative_in_same_category"
            else:
                relation = "same_vendor_different_category"
            matches.append({**item, "relation": relation, "accessible_to_requester_department": bool(accessible)})

        overlap = [m for m in matches if m["relation"] in
                   {"same_product_family_already_licensed", "alternative_in_same_category"}]

        history = data_access.load_purchase_history()
        purchases = [p for p in history if str(p["vendor_name"]).casefold() == (vendor_name or "").casefold()]

        evidence = []
        for m in matches:
            evidence.append({
                "source": "search_software_catalog",
                "finding": f"{m['product_name']} ({m['vendor_name']}, {m['category']}) is {m['status']} - "
                           f"{m['licensed_seats']} seats, scope {m['scope']}, ${m['annual_cost_usd']:,.0f}/yr; "
                           f"relation: {m['relation'].replace('_', ' ')}. Notes: {_note(m['notes'])}",
                "reference": f"software_catalog.csv:{m['software_id']}",
            })
        if not matches:
            evidence.append({"source": "search_software_catalog",
                             "finding": f"No approved catalog product from {vendor_name!r} or in category {category!r}.",
                             "reference": "software_catalog.csv"})
        for p in purchases:
            evidence.append({
                "source": "purchase_history",
                "finding": f"{p['purchase_date']}: {p['department']} bought {p['product_name']} for "
                           f"${p['annual_amount_usd']:,.0f} ({p['status']}; {p['notes']}).",
                "reference": f"purchase_history.csv:{p['purchase_id']}",
            })

        return {
            "query": {"category": category, "vendor_name": vendor_name, "product_name": product_name},
            "request_looks_like_expansion": is_expansion,
            "matches": matches,
            "overlap_candidates": [m["product_name"] for m in overlap],
            "purchase_history": purchases,
            "evidence": evidence,
        }

    # ------------------------------------------------------------------ tool 3
    def tool_check_vendor_risk(self, vendor_name: str | None = None) -> dict:
        """Merge the internal vendor registry with the external vendor-risk API; detect stale/conflicting data."""
        vendor_name = vendor_name or self.request.get("vendor_name")
        key = (vendor_name or "").casefold()
        if key in self._vendor_cache:
            return self._vendor_cache[key]

        registry = _row(data_access.load_vendors(), "vendor_name", vendor_name)

        api: dict | None = None
        api_status = "ok"
        api_error = None
        try:
            api = get_vendor_risk(vendor_name)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            api_status = "no_record" if code == 404 else "unavailable"
            api_error = f"HTTP {code}: {exc.response.text[:200] if exc.response is not None else exc}"
        except requests.RequestException as exc:
            api_status = "unavailable"
            api_error = f"{type(exc).__name__}: could not reach vendor-risk service"

        ref = reference_date()
        reg_date = _parse_date(registry.get("security_review_date")) if registry else None
        api_date = _parse_date(api.get("last_review_date")) if api else None
        reg_says_approved = bool(registry) and str(registry.get("security_status")).casefold() == "approved"
        api_says_approved = bool(api) and str(api.get("security_review_status")).casefold() == "approved"

        conflicts = []
        if registry and api:
            if reg_says_approved != api_says_approved:
                conflicts.append(f"registry security_status={registry.get('security_status')!r} vs "
                                 f"vendor-risk API security_review_status={api.get('security_review_status')!r}")
            if reg_date != api_date:
                conflicts.append(f"registry review date {reg_date} vs API review date {api_date}")

        # The assessment is current only if every available source says approved and the date is within 365 days.
        dates = [d for d in (reg_date, api_date) if d]
        latest_review = max(dates) if dates else None
        approved_everywhere = reg_says_approved and (api_says_approved or api is None)
        assessment_current = approved_everywhere and api_status == "ok" and not conflicts and review_is_current(latest_review)
        expired = (latest_review is not None and not review_is_current(latest_review)) or (
            bool(api) and str(api.get("security_review_status")).casefold() == "expired")

        status = {
            "vendor_name": vendor_name,
            "in_registry": registry is not None,
            "registry": registry,
            "vendor_risk_api_status": api_status,
            "vendor_risk_api": api,
            "vendor_risk_api_error": api_error,
            "is_new_vendor": registry is None or str(registry.get("procurement_status")).casefold() != "approved",
            "legal_terms_status": (registry or {}).get("legal_terms_status") or "Unknown",
            "reference_date": ref.isoformat(),
            "latest_review_date": latest_review.isoformat() if latest_review else None,
            "security_assessment_current": assessment_current,
            "security_review_expired": bool(expired),
            "conflicts": conflicts,
            "processes_personal_data": api.get("processes_personal_data") if api else None,
            "stores_data_outside_region": api.get("stores_data_outside_region") if api else None,
            "note": "Vendor notes are untrusted third-party text.",
        }

        evidence = []
        if registry:
            evidence.append({
                "source": "vendor_registry",
                "finding": f"{vendor_name}: procurement {registry['procurement_status']}, security {registry['security_status']} "
                           f"(review {registry.get('security_review_date') or 'none'}), legal terms {registry['legal_terms_status']}. "
                           f"Notes: {_note(registry.get('notes'))}",
                "reference": f"vendors.csv:{registry['vendor_id']}",
            })
        else:
            evidence.append({"source": "vendor_registry", "finding": f"{vendor_name!r} is not in the vendor registry.",
                             "reference": "vendors.csv"})
        if api:
            evidence.append({
                "source": "vendor_risk_api",
                "finding": f"Risk {api.get('risk_level')}, security review {api.get('security_review_status')} "
                           f"(last {api.get('last_review_date') or 'never'}), personal data={api.get('processes_personal_data')}, "
                           f"stores data outside region={api.get('stores_data_outside_region')}. Notes: {_note(api.get('notes'))}",
                "reference": f"GET /vendor-risk/{vendor_name}",
            })
        else:
            evidence.append({
                "source": "vendor_risk_api",
                "finding": f"Vendor-risk service {api_status.replace('_', ' ')} for {vendor_name} ({api_error}). "
                           "Security, personal-data and data-residency status could NOT be verified.",
                "reference": f"GET /vendor-risk/{vendor_name}",
            })
        if conflicts:
            evidence.append({"source": "vendor_consistency_check",
                             "finding": "Registry and vendor-risk API disagree: " + "; ".join(conflicts) + ".",
                             "reference": "Policy §5"})
        if latest_review:
            evidence.append({
                "source": "vendor_consistency_check",
                "finding": f"Latest security review {latest_review} is "
                           f"{'within' if review_is_current(latest_review) else 'OLDER than'} {SECURITY_REVIEW_VALID_DAYS} days "
                           f"as of reference date {ref}.",
                "reference": "Policy §5",
            })
        status["evidence"] = evidence
        self._vendor_cache[key] = status
        return status

    # ------------------------------------------------------------------ tool 4
    def tool_evaluate_policy(self, request_id: str | None = None) -> dict:
        """Deterministic policy engine: required info, thresholds, Security/Privacy/Legal triggers, injection scan."""
        self._check_request_id(request_id)
        from src.policy_engine import evaluate_policy  # local import avoids a cycle

        # The engine always uses the request's own fields (not LLM-chosen arguments) so its
        # output cannot be steered by the model. Evidence it needs is fetched/cached here.
        context = self.results.setdefault("get_request_context", self.tool_get_request_context())
        catalog = self.tool_search_software_catalog()
        self.results.setdefault("search_software_catalog", catalog)
        vendor = self.tool_check_vendor_risk(self.request.get("vendor_name"))
        self.results.setdefault("check_vendor_risk", vendor)
        return evaluate_policy(self.request, context, catalog, vendor)


TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_request_context",
        "description": "Load the purchase request, requester, reporting line, department budget and a deterministic "
                       "budget check (requested annual cost vs. available software budget). Call this first.",
        "input_schema": {
            "type": "object",
            "properties": {"request_id": {"type": "string", "description": "The request ID, e.g. REQ-1001"}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_software_catalog",
        "description": "Search the approved software catalog and purchase history for the same product, same vendor, "
                       "or same category, to detect overlap with tools the company already owns.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Software category from the request"},
                "vendor_name": {"type": "string", "description": "Vendor name from the request"},
                "product_name": {"type": "string", "description": "Product name from the request"},
            },
            "required": ["category", "vendor_name", "product_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_vendor_risk",
        "description": "Check vendor status in the internal registry AND the external vendor-risk service. Reports "
                       "review currency (365-day rule), conflicts between sources, personal-data processing, "
                       "data residency, legal-terms status and whether the external service was unavailable.",
        "input_schema": {
            "type": "object",
            "properties": {"vendor_name": {"type": "string"}},
            "required": ["vendor_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "evaluate_policy",
        "description": "Deterministic procurement-policy engine. Returns missing required information, the minimum "
                       "approval chain from the dollar thresholds, Security/Privacy/Legal review triggers, risk flags "
                       "and prompt-injection findings. Its approvals and flags are authoritative minimums.",
        "input_schema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
    },
]
