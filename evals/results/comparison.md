# Architecture comparison

Mode: `rules_only` | Model: `None:None` | Cases: 10 | Trials: 0

| Metric | rules_only |
|---|---:|
| runs | 10 |
| pass_rate | 10/10 |
| recommendation_accuracy | 10/10 |
| avg_latency_ms | 25.0 |
| p50_latency_ms | 27.0 |
| max_latency_ms | 40.0 |
| avg_llm_calls | 0.0 |
| avg_tool_calls | 1.0 |
| avg_input_tokens | 0.0 |
| avg_output_tokens | 0.0 |
| est_cost_per_request_usd | 0.0 |
| guardrail_overrides | 0 |
| runs_with_unsupported_figures | 0 |
| modes | ['rules_only'] |
| failed_cases | [] |

## Per-case results

| Variant | Trial | Case | Rec. type | Pass | Latency ms | LLM | Tools | Overrides | Failures |
|---|---:|---|---|---|---:|---:|---:|---|---|
| rules_only | 1 | EXT-01 | proceed_to_approval_routing | PASS | 39.8 | 0 | 1 |  |  |
| rules_only | 1 | EXT-02 | proceed_to_approval_routing | PASS | 12.2 | 0 | 1 |  |  |
| rules_only | 1 | EXT-03 | proceed_to_approval_routing | PASS | 23.4 | 0 | 1 |  |  |
| rules_only | 1 | EXT-04 | consider_existing_tool | PASS | 30.7 | 0 | 1 |  |  |
| rules_only | 1 | EXT-05 | requires_budget_exception | PASS | 13.1 | 0 | 1 |  |  |
| rules_only | 1 | EXT-06 | request_clarification | PASS | 32.2 | 0 | 1 |  |  |
| rules_only | 1 | EXT-07 | escalate_for_manual_review | PASS | 35.0 | 0 | 1 |  |  |
| rules_only | 1 | EXT-08 | consider_existing_tool | PASS | 15.0 | 0 | 1 |  |  |
| rules_only | 1 | EXT-09 | escalate_for_manual_review | PASS | 32.8 | 0 | 1 |  |  |
| rules_only | 1 | EXT-10 | proceed_to_approval_routing | PASS | 16.0 | 0 | 1 |  |  |
