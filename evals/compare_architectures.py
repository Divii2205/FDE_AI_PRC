"""Run Architecture A (single) and B (staged) on the same test set and compare them.

    python evals/compare_architectures.py                # public + extended cases, both architectures
    python evals/compare_architectures.py --trials 3     # repeat each case to measure run-to-run variance

Writes evals/results/comparison.md, comparison.json and per-run rows in runs.csv.
A deterministic rules-only baseline is included so the value added by the LLM is visible.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.mock_api_server import ensure_mock_api
from evals.run_public_evals import evaluate, group_present
from src.config import llm_mode, model_name, provider
from src.data_access import get_request
from src.solution import handle_request

OUT = ROOT / "evals" / "results"
# List price per 1M tokens (input, output) - for cost estimates only; free-tier models count as $0.
PRICE_PER_MTOK = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
NUMBER_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d{4}-\d{2}-\d{2}\b")


def _num_tokens(text: str) -> set[str]:
    return {m.group(0).replace("$", "").replace(",", "").strip().removesuffix(".0") for m in NUMBER_RE.finditer(text)}


def extended_checks(decision, expectations: dict) -> list[str]:
    failures = evaluate(decision, expectations)
    allowed = expectations.get("recommendation_type_in")
    if allowed and decision.recommendation_type not in allowed:
        failures.append(f"recommendation_type {decision.recommendation_type} not in {allowed}")
    for token in expectations.get("required_approvals_must_not_contain", []):
        if group_present(decision.required_approvals, [token]):
            failures.append(f"over-escalation: unexpected approval {token}")
    return failures


def unsupported_figures(decision, request: dict) -> list[str]:
    """Dollar amounts / dates written by the LLM that appear nowhere in tool evidence or the request."""
    grounded = " ".join(e.finding for e in decision.evidence if e.source != "copilot_analysis")
    grounded += " " + json.dumps(request)
    known = _num_tokens(grounded) | {str(v) for v in request.values() if isinstance(v, (int, float))}
    # allow derived shortfalls/totals that equal a difference of two known numbers is overkill; keep it strict
    llm_text = decision.recommendation + " " + decision.next_step + " " + " ".join(
        e.finding for e in decision.evidence if e.source == "copilot_analysis")
    return sorted(_num_tokens(llm_text) - known)


def run_variant(label: str, architecture: str, cases: list[dict], trials: int, force_rules: bool,
                pause: float = 0.0) -> list[dict]:
    rows = []
    previous = os.environ.get("LLM_MODE")
    if force_rules:
        os.environ["LLM_MODE"] = "rules_only"
    try:
        for trial in range(1, trials + 1):
            for case in cases:
                if pause and not force_rules:
                    time.sleep(pause)  # free-tier rate limits; excluded from measured latency
                start = time.perf_counter()
                try:
                    decision = handle_request(case["request_id"], architecture=architecture)
                    latency = (time.perf_counter() - start) * 1000
                    failures = extended_checks(decision, case["expectations"])
                    tel = decision.telemetry
                    unsupported = unsupported_figures(decision, get_request(case["request_id"]))
                    rows.append({
                        "variant": label, "architecture": architecture, "trial": trial,
                        "case_id": case["case_id"], "request_id": case["request_id"], "edge_case": case.get("edge_case", ""),
                        "passed": not failures, "recommendation_type": decision.recommendation_type,
                        "rec_type_ok": decision.recommendation_type in case["expectations"].get("recommendation_type_in", [decision.recommendation_type]),
                        "latency_ms": round(latency, 1), "mode": tel.mode, "llm_calls": tel.llm_calls,
                        "tool_calls": tel.tool_calls, "input_tokens": tel.input_tokens, "output_tokens": tel.output_tokens,
                        "guardrail_overrides": " | ".join(tel.guardrail_overrides),
                        "unsupported_figures": " ".join(unsupported),
                        "approvals": ", ".join(decision.required_approvals), "risk_flags": ", ".join(decision.risk_flags),
                        "failures": " | ".join(failures),
                    })
                    status = "PASS" if not failures else "FAIL"
                    print(f"[{label:>11}] t{trial} {status} {case['case_id']} {decision.recommendation_type:<28} "
                          f"{latency:7.0f} ms llm={tel.llm_calls} tools={tel.tool_calls}"
                          + (f"  overrides={len(tel.guardrail_overrides)}" if tel.guardrail_overrides else ""))
                    for f in failures:
                        print(f"              - {f}")
                except Exception as exc:  # keep going; an error is a failed case
                    rows.append({"variant": label, "architecture": architecture, "trial": trial, "case_id": case["case_id"],
                                 "request_id": case["request_id"], "passed": False, "rec_type_ok": False,
                                 "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                                 "failures": f"ERROR {type(exc).__name__}: {exc}"})
                    print(f"[{label:>11}] t{trial} ERROR {case['case_id']} {type(exc).__name__}: {exc}")
    finally:
        if force_rules:
            if previous is None:
                os.environ.pop("LLM_MODE", None)
            else:
                os.environ["LLM_MODE"] = previous
    return rows


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    lat = [r["latency_ms"] for r in rows]
    def avg(key):
        vals = [r.get(key) or 0 for r in rows]
        return round(sum(vals) / n, 2) if n else 0
    in_price, out_price = PRICE_PER_MTOK.get(model_name() or "", (0, 0))
    cost = sum((r.get("input_tokens") or 0) * in_price + (r.get("output_tokens") or 0) * out_price for r in rows) / 1e6
    return {
        "runs": n,
        "pass_rate": f"{sum(r['passed'] for r in rows)}/{n}",
        "recommendation_accuracy": f"{sum(bool(r.get('rec_type_ok')) for r in rows)}/{n}",
        "avg_latency_ms": round(statistics.mean(lat), 0) if lat else 0,
        "p50_latency_ms": round(statistics.median(lat), 0) if lat else 0,
        "max_latency_ms": round(max(lat), 0) if lat else 0,
        "avg_llm_calls": avg("llm_calls"),
        "avg_tool_calls": avg("tool_calls"),
        "avg_input_tokens": avg("input_tokens"),
        "avg_output_tokens": avg("output_tokens"),
        "est_cost_per_request_usd": round(cost / n, 4) if n else 0,
        "guardrail_overrides": sum(1 for r in rows if r.get("guardrail_overrides")),
        "runs_with_unsupported_figures": sum(1 for r in rows if r.get("unsupported_figures")),
        "modes": sorted({r.get("mode") or "error" for r in rows}),
        "failed_cases": sorted({r["case_id"] for r in rows if not r["passed"]}),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--no-rules-baseline", action="store_true")
    parser.add_argument("--pause", type=float, default=0.0,
                        help="seconds to wait between LLM cases (free-tier rate limits)")
    args = parser.parse_args()

    ensure_mock_api()
    cases = json.loads((ROOT / "evals" / "extended_cases.json").read_text(encoding="utf-8"))
    mode = llm_mode()
    print(f"\nComparing architectures on {len(cases)} cases x {args.trials} trial(s); mode={mode}, model={provider()}:{model_name()}\n")

    rows: list[dict] = []
    if not args.no_rules_baseline:
        rows += run_variant("rules_only", "single", cases, 1, force_rules=True)
    if mode != "llm":
        print("\nWARNING: no LLM provider configured - A and B would both fall back to the rules-only path.\n"
              "Only the deterministic baseline was run. Add a key to .env (see README) for the real comparison.\n")
        args.trials = 0
    rows += run_variant("A_single", "single", cases, args.trials, force_rules=False, pause=args.pause)
    rows += run_variant("B_staged", "staged", cases, args.trials, force_rules=False, pause=args.pause)

    variants = list(dict.fromkeys(r["variant"] for r in rows))
    summary = {v: summarize([r for r in rows if r["variant"] == v]) for v in variants}

    OUT.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with (OUT / "runs.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    template_rows = []
    for r in rows:
        failures = r.get("failures", "")
        policy_fail = [f for f in failures.split(" | ") if f and not f.startswith("recommendation_type")]
        template_rows.append({
            "case_id": r["case_id"], "architecture": r["variant"],
            "correct_next_action": bool(r.get("rec_type_ok")),
            "grounded_evidence": not r.get("unsupported_figures") and "insufficient evidence" not in failures,
            "policy_followed": not policy_fail,
            "human_escalation_correct": bool(r.get("rec_type_ok")) and "human_review_required" not in failures,
            "latency_ms": r["latency_ms"], "llm_calls": r.get("llm_calls", ""), "tool_calls": r.get("tool_calls", ""),
            "notes": "; ".join(x for x in (failures, r.get("guardrail_overrides", ""),
                                           ("unsupported figures: " + r["unsupported_figures"]) if r.get("unsupported_figures") else "") if x),
        })
    with (OUT / "evaluation_results.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(template_rows[0]))
        writer.writeheader()
        writer.writerows(template_rows)

    (OUT / "comparison.json").write_text(json.dumps({"mode": mode, "model": f"{provider()}:{model_name()}", "trials": args.trials,
                                                      "summary": summary}, indent=2), encoding="utf-8")

    metrics = list(next(iter(summary.values())).keys())
    lines = [f"# Architecture comparison\n", f"Mode: `{mode}` | Model: `{provider()}:{model_name()}` | Cases: {len(cases)} | Trials: {args.trials}\n",
             "| Metric | " + " | ".join(variants) + " |", "|---|" + "---:|" * len(variants)]
    for m in metrics:
        lines.append(f"| {m} | " + " | ".join(str(summary[v][m]) for v in variants) + " |")
    lines += ["", "## Per-case results", "", "| Variant | Trial | Case | Rec. type | Pass | Latency ms | LLM | Tools | Overrides | Failures |",
              "|---|---:|---|---|---|---:|---:|---:|---|---|"]
    for r in rows:
        lines.append(f"| {r['variant']} | {r['trial']} | {r['case_id']} | {r.get('recommendation_type', '')} | "
                     f"{'PASS' if r['passed'] else 'FAIL'} | {r['latency_ms']} | {r.get('llm_calls', '')} | "
                     f"{r.get('tool_calls', '')} | {r.get('guardrail_overrides', '')} | {r.get('failures', '')} |")
    (OUT / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\nSummary")
    for v in variants:
        print(f"  {v}: {summary[v]}")
    print(f"\nWritten: {OUT.relative_to(ROOT)}/comparison.md, comparison.json, runs.csv, evaluation_results.csv")


if __name__ == "__main__":
    main()
