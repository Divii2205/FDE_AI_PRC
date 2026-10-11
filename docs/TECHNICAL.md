# AI Procurement Request Copilot - technical reference

> This is the full technical README. The beginner-friendly overview is in [`../README.md`](../README.md).

An internal tool that reviews software and service purchase requests. It gathers evidence with tools, applies the procurement policy in deterministic code, uses an LLM to interpret the request and recommend the next action, and **always leaves the decision to a human**.

FDE Assessment 3 submission. All data is synthetic. The original starter-pack README is in [`STARTER_README.md`](STARTER_README.md).

| | |
|---|---|
| Architecture & diagrams | [`architecture.md`](architecture.md) |
| Decision memo (≤500 words) | [`decision_memo.md`](decision_memo.md) |
| Evaluation output | [`evals/results/`](../evals/results/) |

---

## 1. Setup and run

Requires Python 3.11+ (tested on 3.13).

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1      macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python verify_setup.py                     # expect: PRE-FLIGHT PASSED
cp .env.example .env                       # Windows: Copy-Item .env.example .env
```

**Add one LLM key to `.env`.** Free options work:

| Provider | `.env` entry | Default model (override with `MODEL_NAME`) |
|---|---|---|
| Google Gemini (AI Studio free tier) | `GEMINI_API_KEY=...` | `gemini-3.8-flash` |
| Groq (free tier) | `GROQ_API_KEY=...` | `llama-3.3-70b-versatile` |
| OpenRouter free models | `OPENROUTER_API_KEY=...` | `meta-llama/llama-3.3-70b-instruct:free` |
| Ollama (local, no key) | `LLM_PROVIDER=ollama` | `qwen2.5:7b` |
| Anthropic Claude | `ANTHROPIC_API_KEY=...` | `claude-opus-5-5` |

With no key the app still works in a clearly labelled **rules-only mode** (no LLM). This mode is also the fallback whenever an LLM call fails.

**One-command start** (mock vendor-risk API on :8001 plus the UI on :8501):

```bash
python run_local.py
```

Then open http://127.0.0.1:8501.

**Tests and evaluations:**

```bash
python -m unittest discover -s tests -t .              # 34 tests, no key or server needed
python evals/run_public_evals.py --architecture single # public harness (starts the mock API if needed)
python evals/run_public_evals.py --architecture staged
python evals/compare_architectures.py --trials 3       # A vs B on all 10 cases, plus a rules-only baseline
#   free tiers: add --pause 5 to stay under rate limits (the wait is excluded from measured latency),
#   e.g. python evals/compare_architectures.py --trials 1 --pause 5
```

## 2. Product workflow

1. **Request.** Pick an existing request, or submit a new one from the sidebar form.
2. **Understand.** The agent reads the request. All request and vendor text is treated as untrusted data.
3. **Gather evidence.** Four tools cover budget, existing tools, vendor risk (internal registry plus external API), and policy.
4. **Recommend.** You get:
   - a recommendation and next step
   - required approvals
   - risk flags
   - missing information
   - an evidence table showing the source of every finding
   - run details: latency, LLM calls, tool calls, tokens, and any guardrail overrides
5. **Human review.** A reviewer records *send to approvers*, *return to requester*, *escalate*, or *decline*, with notes, in `review_log/decisions.jsonl`. The copilot never purchases, approves, or edits budgets.

Choose **compare A vs B** in the sidebar to run both architectures on the same request side by side.

## 3. Architecture

| | A - single agent (baseline) | B - staged / 2 agents |
|---|---|---|
| Flow | one LLM agent → 4 tools → decision JSON | Analyst LLM (3 evidence tools) → evidence pack → policy engine (code) → Reviewer LLM (no tools) |
| Code | `src/agents/single.py` | `src/agents/staged.py` |

Both architectures share the same tools (`src/tools.py`), policy engine (`src/policy_engine.py`), guardrail merge (`src/agents/common.py`), and LLM layer (`src/llm.py`). The LLM layer has one adapter for Claude, which enforces JSON output with `output_config`, and one for any OpenAI-compatible endpoint (Gemini, Groq, OpenRouter, Ollama), which parses tolerant JSON and makes one repair call if needed. The `handle_request(request_id, architecture)` adapter in `src/solution.py` is unchanged for the graders.

**What is deterministic and what is AI:**
- **Code** handles:
  - every threshold, budget, date, overlap and review-trigger rule
  - recommendation floors (it overrides an LLM that tries to "proceed" past missing information or a vendor outage)
  - `human_review_required = true`
- **The LLM** handles:
  - overlap judgement (is there a credible gap?)
  - ambiguity
  - choosing among the allowed recommendations
  - the rationale and next-step wording
- **The LLM can add** approvals, flags, or missing items. **It cannot remove them.**

Full details, diagrams, and stop/escalation conditions are in [`architecture.md`](architecture.md).

### Tools (all 4 are deterministic, non-LLM)

| Tool | Purpose |
|---|---|
| `get_request_context` | Requester, reporting line, department budget, and a budget check |
| `search_software_catalog` | Same-vendor and same-category matches, expansion vs. duplicate, purchase history |
| `check_vendor_risk` | Merges the registry with the external API: 365-day currency, conflicts, data residency, legal terms. Outage, 404, and timeout are all reported as unverifiable |
| `evaluate_policy` | Policy §1–§9: missing information, approval chain, Security/Privacy/Legal triggers, injection scan, recommendation floor |

### Fixes to the starter scaffold

- `vendor_client.get_vendor_risk` raised on 503, 404, or connection refused. `check_vendor_risk` now turns each of these into an explicit "unverifiable" status.
- The eval runner assumed the mock API was already running. `evals/mock_api_server.py` now starts it on demand.
- `data_access_level: "unknown"` is now treated as missing information (policy §1).
- `requirements.txt` had no LLM client. Gemini, Groq, OpenRouter and Ollama are now called over plain HTTPS with `requests`, and `anthropic` was added as an optional extra for Claude. Streamlit's deprecated `use_container_width` was replaced.
- `run_local.py` waited only 10 s for the API. It now waits up to 60 s with progress messages, reuses an API that is already running, reports a busy port clearly, and bypasses system proxies for localhost.
- On a fresh machine, Streamlit stopped at its first-run email prompt and the UI process died when `run_local.py` ran non-interactively. The launcher now starts Streamlit headless.
- Date checks use the policy reference date (2026-09-30), which is parsed from `procurement_policy.md`. The machine clock is never used.

## 4. Assumptions

- `annual_cost_usd` is the annualised amount used for the thresholds.
- Approvals are expressed as roles (Manager, Department Head, …), not named people.
- A request is an **expansion** rather than overlap when it is same-vendor and contains wording like "expansion", "add-on" or "additional seats".
- A same-vendor, same-category product already licensed company-wide or to the requester's department counts as **overlap**.
- An unknown vendor (404 from the risk API) is treated exactly like an outage.
- Data stored outside the region triggers Privacy for any data level above `none`. It also triggers Legal when the data is PII or confidential.

The full list is in [`architecture.md`](architecture.md#assumptions).

## 5. Evaluation

The **same 10-case set** (`evals/extended_cases.json`) is run on both architectures. It covers every edge case the brief names:

| Edge case | Cases |
|---|---|
| incomplete request + prompt injection | EXT-06 |
| existing tool already solves the need | EXT-08, EXT-02 |
| conflicting / expired vendor info | EXT-07 |
| security-sensitive + approval thresholds | EXT-03, EXT-05 |
| API unavailable | EXT-09 |
| AI tool, new data class | EXT-04 |
| ambiguity | EXT-10 |

The extended set is stricter than the public one. Each case checks:
- the recommendation type
- **over-escalation**, e.g. adding CFO or Security where it isn't needed
- unsupported figures: any $ amount or date the LLM writes that appears nowhere in the tool evidence
- policy compliance
- human review
- latency, LLM calls, tool calls and tokens

**Current results** ([`evals/results/comparison.md`](../evals/results/comparison.md)):

| Metric | Rules-only baseline | A - single | B - staged |
|---|---:|---:|---:|
| Cases passing all checks | 10/10 | *pending LLM key* | *pending LLM key* |
| Recommendation accuracy | 10/10 | – | – |
| Avg latency | ~25 ms | – | – |
| Avg LLM calls / tool calls | 0 / 1 | – | – |
| Public harness (6 cases) | 6/6 | 6/6* | 6/6* |

\* These runs were in rules-only mode because no LLM key was configured on the build machine. To fill in the A and B columns, add a key and run `python evals/compare_architectures.py --trials 3`, then update this table and the memo.

## 6. Architecture comparison and ship decision

See [`decision_memo.md`](decision_memo.md).

## 7. Troubleshooting (Windows)

**`Vendor-risk API did not become ready` or `DLL load failed ... An Application Control policy has blocked this file`.**
Windows **Smart App Control** blocks unsigned compiled Python modules, and the first run is slowed while it checks each new file. The project avoids the binaries it was seen blocking:
- pandas/pyarrow was replaced with the built-in `csv` module
- the evidence table renders without pyarrow
- the free providers are called with `requests` instead of the `openai` SDK, which needs the blocked `jiter` module

Even so, on a fresh machine the first `python run_local.py` can take up to a minute; just let it finish. To check whether it's enabled, look at Windows Security → App & browser control → Smart App Control. The Claude path (`anthropic` SDK) also loads `jiter`, so on such machines use a free provider.

**`404 ... model is no longer available`.** Provider model names change. List the models your key can use and set `MODEL_NAME` in `.env`. For Gemini: `GET https://generativelanguage.googleapis.com/v1beta/openai/models`.

**Port 8001/8501 in use.** An earlier run is still alive. Close that terminal, or run `Get-NetTCPConnection -LocalPort 8501 | % { Stop-Process -Id $_.OwningProcess -Force }`.

## 8. Known limitations

- The LLM comparison numbers depend on the model and provider. Free-tier models are weaker and rate-limited; quota exhaustion falls back to rules-only and is reported in telemetry.
- Overlap detection is keyword- and category-based. A tool that solves the same need under a different category name is left to the LLM to spot.
- The prompt-injection scan is pattern-based. The real defence is architectural: business text never controls the rules, and the LLM cannot remove controls. A novel phrasing could therefore go unflagged, but it cannot bypass the policy.
- The data is a CSV snapshot. There is no live ERP, ticketing, or identity integration, and the human review log is a local JSONL file with no authentication.
- With small samples (10 cases × 3 trials), small differences in accuracy between A and B aren't statistically meaningful.

## Retaking the README screenshots

`tools/capture_screenshots.py` drives the installed Microsoft Edge (or Chrome) headless over the DevTools protocol. Nothing extra is installed, so it also works under Windows Smart App Control. The app supports deep links that open a request and run the check automatically: `?request=REQ-1006&view=single|staged|compare&run=1`.

```powershell
python run_local.py                      # in one terminal
python tools/capture_screenshots.py --base-url http://127.0.0.1:8501 --out docs/screenshots `
  --shot "01-home||Check this request" `
  --shot "02-good-to-go|?request=REQ-1001&view=single&run=1|What should happen next"
```

Each AI check uses 3–4 calls. Gemini's free tier allows about 20 calls per model per day, so spread a full set across models with `MODEL_NAME` if needed. The current images used `gemini-3.5-flash` (shots 2–4) and `gemini-3.1-flash-lite` (shots 5–6).
