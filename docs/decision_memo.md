# Architecture Decision Memo

> **Status: DRAFT.** The LLM columns are still empty because the build machine had no LLM key. Run
> `python evals/compare_architectures.py --trials 3`, copy the numbers from `evals/results/comparison.md`,
> and confirm or revise the decision below.

## Decision
Ship **Architecture A, the single agent**, on top of the deterministic policy engine and guardrail merge.
Architecture B stays in the codebase and can be switched on with one parameter if the evidence later justifies it.

## Evidence
Both architectures run on the same 10 cases (`evals/extended_cases.json`), 3 trials each. The cases cover every
edge case the brief lists.

| Metric | Rules-only | Single agent | Staged / 2-agent |
|---|---:|---:|---:|
| Cases passing all checks | 10/10 | _ | _ |
| Recommendation accuracy | 10/10 | _ | _ |
| Avg latency | ~25 ms | _ | _ |
| Avg LLM calls | 0 | _ | _ |
| Avg tool calls | 1 | _ | _ |
| Guardrail overrides (LLM corrected by code) | 0 | _ | _ |
| Runs with unsupported figures | 0 | _ | _ |

## Trade-offs
Every control that grading depends on lives in tested code, not prompts: approval thresholds, budget,
the 365-day review rule, registry/API conflicts, Security/Privacy/Legal triggers, missing information,
injection flags, recommendation floors, and mandatory human review. As a result:

- **Both architectures share the same floor for correctness.** Neither can drop an approval or proceed
  past missing information or an outage. The guardrail-override count shows how often the code had to step in.
- **The LLM adds judgement, not compliance.** It decides whether an existing tool truly covers the need
  (REQ-1002, REQ-1004: limited-use AI approval vs. customer PII), flags ambiguity (REQ-1010), and writes
  next steps a reviewer can act on.
- **B costs more by construction.** It always adds a reviewer LLM call and a serial hop, plus a handoff
  where evidence can be lost. Its possible upside is sharper overlap and ambiguity judgement from a curated
  evidence pack, which is the hypothesis the evaluation tests.

## Risks / limitations
- The results depend on the model. Free-tier models are weaker and rate-limited, so re-run the comparison on the production model before rollout.
- Ten cases × three trials can't detect small accuracy differences. Expand the case set with real historical requests and reviewer-labelled outcomes.
- Before production, validate:
  - injection resistance against adversarial vendor notes
  - behaviour when the catalog and registry are stale
  - reviewer agreement rates in a shadow-mode pilot

## Why this is the right MVP
The client's problem is consistent, auditable triage with humans keeping authority. Determinism, not
orchestration, delivers that. Once the rules sit in code, one agent with four tools covers the whole
workflow at the lowest latency and cost, with one reasoning trace to debug.

A second agent is only worth its extra latency and cost if the evaluation shows it makes better
decisions. If the LLM results show B improving recommendation accuracy on the judgement cases (overlap,
AI data class, ambiguity) by more than run-to-run noise, revisit this decision. Otherwise the simpler
system wins, as the brief advises.
