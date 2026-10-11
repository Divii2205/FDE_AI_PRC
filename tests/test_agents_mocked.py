"""Exercise both agent loops and the guardrails with a scripted fake Claude client (no API key needed)."""
from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from src import llm, tools
from src.data_access import get_request
from src.pipeline import analyze
from tests.helpers import in_process_vendor_risk


def _usage():
    return NS(input_tokens=100, output_tokens=50)


def tool_use(*calls):
    blocks = [NS(type="tool_use", id=f"tu_{i}", name=n, input=a) for i, (n, a) in enumerate(calls)]
    return NS(content=blocks, stop_reason="tool_use", usage=_usage())


def final(payload: dict):
    return NS(content=[NS(type="text", text=json.dumps(payload))], stop_reason="end_turn", usage=_usage())


def decision(rec_type, **extra):
    base = {"recommendation_type": rec_type, "recommendation": "LLM recommendation.", "rationale": "Because evidence.",
            "overlap_assessment": "n/a", "additional_approvals": [], "additional_risk_flags": [],
            "additional_missing_information": [], "next_step": "LLM next step.", "untrusted_instructions_ignored": []}
    base.update(extra)
    return base


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        create = self._create
        self.messages = NS(create=create)
        self.beta = NS(messages=NS(create=create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)


def run_with(script, request_id, architecture):
    fake = FakeClient(script)
    with mock.patch.dict(os.environ, {"LLM_MODE": "", "LLM_PROVIDER": "anthropic", "MODEL_NAME": "claude-opus-5-5"}), \
         mock.patch.object(llm, "anthropic_client", lambda: fake), \
         mock.patch.object(tools, "get_vendor_risk", in_process_vendor_risk):
        return analyze(get_request(request_id), architecture), fake


ALL_TOOLS = tool_use(("search_software_catalog", {"category": "x", "vendor_name": "x", "product_name": "x"}),
                     ("check_vendor_risk", {"vendor_name": "NeuralDesk"}))


# ---------------------------------------------------------------- OpenAI-compatible REST (Gemini/Groq/...) fakes
def _resp(message):
    return {"choices": [{"message": message}], "usage": {"prompt_tokens": 80, "completion_tokens": 20}}


def oa_tool_calls(*calls):
    return _resp({"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
        for i, (n, a) in enumerate(calls)]})


def oa_text(text):
    return _resp({"role": "assistant", "content": text})


class FakeOpenAI:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, name, payload, max_retries=5):
        self.calls.append(json.loads(json.dumps(payload)))  # snapshot: messages list keeps mutating
        return self.script.pop(0)


def run_openai_with(script, request_id, architecture):
    fake = FakeOpenAI(script)
    with mock.patch.dict(os.environ, {"LLM_MODE": "", "LLM_PROVIDER": "groq", "MODEL_NAME": "test-model"}),          mock.patch.object(llm, "post_chat_completion", fake),          mock.patch.object(tools, "get_vendor_risk", in_process_vendor_risk):
        return analyze(get_request(request_id), architecture), fake


class OpenAICompatTests(unittest.TestCase):
    def test_single_agent_with_fenced_json_and_missing_keys(self):
        payload = {"recommendation_type": "consider_existing_tool", "recommendation": "Use TaskFlow.", "next_step": "Check seats."}
        d, fake = run_openai_with([
            oa_tool_calls(("get_request_context", {"request_id": "REQ-1008"}), ("evaluate_policy", {"request_id": "REQ-1008"})),
            oa_text("Here you go:\n```json\n" + json.dumps(payload) + "\n```"),
        ], "REQ-1008", "single")
        self.assertEqual(d.recommendation_type, "consider_existing_tool")
        self.assertEqual(d.telemetry.llm_calls, 2)
        self.assertTrue(d.telemetry.model.startswith("groq:"))
        self.assertEqual(fake.calls[1]["messages"][-1]["role"], "tool")

    def test_repair_call_when_no_json(self):
        d, fake = run_openai_with([
            oa_tool_calls(("evaluate_policy", {"request_id": "REQ-1001"})),
            oa_text("I think this should go to the manager."),
            oa_text(json.dumps(decision("proceed_to_approval_routing"))),
        ], "REQ-1001", "single")
        self.assertEqual(fake.calls[-1]["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", fake.calls[-1])
        self.assertEqual(d.telemetry.mode, "llm")

    def test_staged_on_openai_compat(self):
        pack = {"need_summary": "s", "key_facts": [], "existing_tool_assessment": "a", "credible_gap_stated": False,
                "ambiguities": [], "unverified_items": [], "untrusted_instructions_seen": []}
        d, _ = run_openai_with([oa_tool_calls(("check_vendor_risk", {"vendor_name": "SignalWatch"})),
                                oa_text(json.dumps(pack)), oa_text(json.dumps(decision("escalate_for_manual_review")))],
                               "REQ-1007", "staged")
        self.assertEqual(d.telemetry.llm_stages, ["analyst", "analyst", "reviewer"])
        self.assertIn("conflicting_vendor_evidence", d.risk_flags)


class SingleAgentTests(unittest.TestCase):
    def test_happy_path_counts_calls(self):
        d, fake = run_with([tool_use(("get_request_context", {"request_id": "REQ-1001"})),
                            tool_use(("search_software_catalog", {"category": "E-signature", "vendor_name": "SignFlow",
                                                                  "product_name": "SignFlow Add-on"}),
                                     ("check_vendor_risk", {"vendor_name": "SignFlow"})),
                            tool_use(("evaluate_policy", {"request_id": "REQ-1001"})),
                            final(decision("proceed_to_approval_routing"))], "REQ-1001", "single")
        self.assertEqual(d.telemetry.llm_calls, 4)
        self.assertEqual(d.telemetry.tool_calls, 4)
        self.assertEqual(d.telemetry.mode, "llm")
        self.assertEqual(d.required_approvals, ["Manager"])
        self.assertTrue(d.recommendation.endswith("LLM recommendation."))
        self.assertEqual(fake.calls[0]["output_config"]["format"]["type"], "json_schema")

    def test_llm_cannot_override_missing_info_floor_or_drop_controls(self):
        # Simulates a model that was persuaded by the injected "CFO-approved" text.
        d, _ = run_with([tool_use(("get_request_context", {"request_id": "REQ-1006"})),
                         tool_use(("evaluate_policy", {"request_id": "REQ-1006"})),
                         final(decision("proceed_to_approval_routing", additional_approvals=["CFO"]))],
                        "REQ-1006", "single")
        self.assertEqual(d.recommendation_type, "request_clarification")
        self.assertTrue(d.telemetry.guardrail_overrides)
        self.assertIn("prompt_injection_detected", d.risk_flags)
        self.assertEqual(len(d.missing_information), 3)
        self.assertTrue(d.human_review_required)

    def test_agent_nudged_when_skipping_policy(self):
        d, fake = run_with([final(decision("proceed_to_approval_routing")),
                            tool_use(("evaluate_policy", {"request_id": "REQ-1003"})),
                            final(decision("proceed_to_approval_routing"))], "REQ-1003", "single")
        self.assertIn("Security", d.required_approvals)
        self.assertEqual(len(fake.calls), 3)

    def test_llm_failure_falls_back_to_rules(self):
        refusal = NS(content=[], stop_reason="refusal", usage=_usage())
        d, _ = run_with([refusal], "REQ-1009", "single")
        self.assertEqual(d.telemetry.mode, "rules_only_fallback")
        self.assertIn("vendor_risk_unavailable", d.risk_flags)
        self.assertIn("llm_unavailable", d.risk_flags)

    def test_llm_judgment_on_overlap_is_respected(self):
        d, _ = run_with([tool_use(("evaluate_policy", {"request_id": "REQ-1004"})),
                         final(decision("proceed_to_approval_routing"))], "REQ-1004", "single")
        self.assertEqual(d.recommendation_type, "proceed_to_approval_routing")  # not a floor: LLM may decide
        self.assertIn("Privacy", d.required_approvals)


class StagedTests(unittest.TestCase):
    def test_two_stage_flow(self):
        pack = {"need_summary": "s", "key_facts": [{"source": "get_request_context", "fact": "f"}],
                "existing_tool_assessment": "a", "credible_gap_stated": True, "ambiguities": [],
                "unverified_items": ["vendor risk"], "untrusted_instructions_seen": []}
        d, fake = run_with([tool_use(("get_request_context", {"request_id": "REQ-1009"})),
                            tool_use(("check_vendor_risk", {"vendor_name": "NimbusAI"})),
                            final(pack),
                            final(decision("escalate_for_manual_review"))], "REQ-1009", "staged")
        self.assertEqual(d.telemetry.llm_stages, ["analyst", "analyst", "analyst", "reviewer"])
        self.assertNotIn("tools", fake.calls[-1])  # reviewer has no tools
        self.assertIn("evaluate_policy", d.telemetry.tool_names)
        analyst_tool_names = {t["name"] for t in fake.calls[0]["tools"]}
        self.assertNotIn("evaluate_policy", analyst_tool_names)
        self.assertEqual(d.recommendation_type, "escalate_for_manual_review")


class RestRetryTests(unittest.TestCase):
    def test_retries_rate_limit_then_succeeds(self):
        ok = mock.Mock(status_code=200, headers={}, json=lambda: {"choices": [{"message": {"content": "{}"}}]})
        limited = mock.Mock(status_code=429, headers={"retry-after": "0"}, text="slow down")
        with mock.patch("requests.post", side_effect=[limited, ok]) as post, mock.patch("time.sleep"):
            data = llm.post_chat_completion("groq", {"model": "m", "messages": []})
        self.assertEqual(post.call_count, 2)
        self.assertIn("choices", data)

    def test_quota_exhausted_becomes_llm_error(self):
        limited = mock.Mock(status_code=429, headers={}, text="quota")
        with mock.patch("requests.post", return_value=limited), mock.patch("time.sleep"):
            with self.assertRaises(llm.LLMError):
                llm.post_chat_completion("groq", {"model": "m", "messages": []}, max_retries=2)


class DailyQuotaTests(unittest.TestCase):
    def test_daily_quota_fails_fast_without_retrying(self):
        body = '[{"error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "24936s"}]}}]'
        limited = mock.Mock(status_code=429, headers={}, text=body)
        with mock.patch("requests.post", return_value=limited) as post, mock.patch("time.sleep") as sleep:
            with self.assertRaises(llm.LLMError) as ctx:
                llm.post_chat_completion("gemini", {"model": "gemini-x", "messages": []})
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("daily free-tier quota", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
