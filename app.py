from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from src.config import llm_mode, model_name, provider, reference_date
from src.contracts import ProcurementDecision
from src.data_access import load_employees, load_requests
from src.pipeline import analyze
from src.ui_text import (
    APPROVER_EXPLANATIONS, ARCH_TEXT, DATA_ACCESS_TEXT, GLOSSARY, RECOMMENDATION_HEADLINES, REVIEW_ACTIONS,
    RISK_FLAG_TEXT, URGENCY_TEXT, VIEW_HELP, VIEW_OPTIONS, data_access_label, humanize, source_label,
)

ROOT = Path(__file__).resolve().parent
REVIEW_LOG = ROOT / "review_log" / "decisions.jsonl"

SRC_EXISTING = "Pick an existing request"
SRC_NEW = "Type a new request"
NEXT_STEP_HEADING = "What should happen next"  # screenshot automation waits for this text

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")


def md(text: object) -> str:
    """Escape text for Streamlit markdown: '$' would otherwise start LaTeX, '|' breaks tables."""
    return str(text).replace("$", "\\$").replace("|", "\\|").replace("\n", " ")


@st.cache_data
def requests_by_id() -> dict[str, dict]:
    return {r["request_id"]: r for r in load_requests()}


@st.cache_data
def employees() -> dict[str, dict]:
    return {e["employee_id"]: e for e in load_employees()}


# ---------------------------------------------------------------------------------------- step 1
def show_request(req: dict) -> None:
    st.header("Step 1 · The request")
    person = employees().get(req.get("requester_id"))
    who = f"{person['name']} ({person['department']} team)" if person else (req.get("requester_id") or "Not given")
    cost = req.get("annual_cost_usd")
    users = req.get("user_count")
    integrations = ", ".join(req.get("requested_integrations") or []) or "Nothing"

    st.subheader(md(req.get("product_name") or "Unnamed product"))
    st.caption(f"Request number: {md(req.get('request_id'))}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Cost per year", f"${cost:,.0f}" if cost is not None else "Not given")
    c2.metric("Number of people", users if users is not None else "Not given")
    c3.metric("Urgency", URGENCY_TEXT.get(str(req.get("urgency")), humanize(req.get("urgency"))))

    left, right = st.columns(2)
    with left:
        st.markdown(
            f"**Who is asking:** {md(who)}  \n"
            f"**What they want to buy:** {md(req.get('product_name') or 'Not given')}  \n"
            f"**From which company (vendor):** {md(req.get('vendor_name') or 'Not given')}"
        )
    with right:
        st.markdown(
            f"**Type of software:** {md(req.get('category') or 'Not given')}  \n"
            f"**What data it will touch:** {md(data_access_label(req.get('data_access_level')))}  \n"
            f"**Connects to:** {md(integrations)}"
        )

    st.markdown("**Why they need it (written by the requester)**")
    st.info(md(req.get("business_justification") or "(Nothing written)"))
    st.caption("This text comes from the requester. The tool reads it as information only — "
               "it never follows instructions written in it.")


# ---------------------------------------------------------------------------------------- step 3
def show_decision(d: ProcurementDecision, key: str) -> None:
    kind, headline = RECOMMENDATION_HEADLINES.get(d.recommendation_type or "", ("info", "Advice"))
    sentence = d.recommendation.split(": ", 1)[-1]
    getattr(st, kind)(f"### {headline}\n\n{md(sentence)}")

    with st.container(border=True):
        st.markdown(f"#### ➡️ {NEXT_STEP_HEADING}")
        st.markdown(md(d.next_step))

    c1, c2, c3 = st.columns(3)
    with c1, st.container(border=True):
        st.markdown("#### Who needs to say yes")
        if d.required_approvals:
            for a in d.required_approvals:
                expl = APPROVER_EXPLANATIONS.get(a)
                st.markdown(f"- **{md(a)}** – {expl}" if expl else f"- **{md(a)}**")
        else:
            st.markdown("No approvals listed.")
    with c2, st.container(border=True):
        st.markdown("#### Things to watch out for")
        if d.risk_flags:
            for f in d.risk_flags:
                if f in RISK_FLAG_TEXT:
                    st.markdown(f"- {RISK_FLAG_TEXT[f]}")
                else:
                    st.markdown(f"- {md(humanize(f))}  \n  :gray[`{md(f)}`]")
        else:
            st.markdown("Nothing to worry about ✓")
    with c3, st.container(border=True):
        st.markdown("#### Missing details")
        if d.missing_information:
            for m in d.missing_information:
                st.markdown(f"- {md(m)}")
        else:
            st.markdown("Nothing missing ✓")

    st.markdown("#### Why? The facts behind this advice")
    st.caption("Every fact comes from company data or a system check. "
               "The 'AI summary' row is the AI's own explanation.")
    # Plain markdown table (st.dataframe needs pyarrow, which Application Control can block on Windows)
    rows = ["| Where it comes from | What we found | Reference |", "|---|---|---|"]
    rows += [f"| {md(source_label(e.source))} | {md(e.finding)} | {md(e.reference or '')} |" for e in d.evidence]
    st.markdown("\n".join(rows))

    with st.expander("What do these words mean?"):
        for word, meaning in GLOSSARY:
            st.markdown(f"- **{word}:** {meaning}")

    t = d.telemetry
    if t:
        with st.expander("Technical details (for reviewers)"):
            mode_text = "AI on" if t.mode == "llm" else "Rules only (no AI)"
            st.markdown(
                f"- **Mode:** {md(mode_text)} (`{md(t.mode or '-')}`)\n"
                f"- **How the AI worked:** {md(ARCH_TEXT.get(t.architecture or '', t.architecture or '-'))} "
                f"(`{md(t.architecture or '-')}`)\n"
                f"- **AI model:** {md(t.model or 'None')}\n"
                f"- **Time taken:** {(t.latency_ms or 0) / 1000:.1f} seconds\n"
                f"- **Number of AI calls:** {t.llm_calls or 0}\n"
                f"- **Number of tool calls:** {t.tool_calls or 0}\n"
                f"- **Tools used, in order:** {md(' → '.join(t.tool_names) or 'None')}\n"
                f"- **AI stages:** {md(', '.join(t.llm_stages) or 'None')}\n"
                f"- **Tokens (AI text units):** {t.input_tokens or 0:,} in / {t.output_tokens or 0:,} out"
            )
            st.markdown("**Guardrail overrides** — times the company rules corrected the AI:")
            if t.guardrail_overrides:
                for o in t.guardrail_overrides:
                    st.warning(md(o))
            else:
                st.markdown("None — the rules did not need to correct the AI.")
            st.download_button("Download the full result (JSON)", d.model_dump_json(indent=2),
                               file_name=f"{d.request_id}_{t.architecture}.json", key=f"dl_{key}")


# ---------------------------------------------------------------------------------------- step 4
def human_review(d: ProcurementDecision) -> None:
    st.header("Step 4 · Your decision")
    st.caption("The copilot only gives advice. It never buys anything or approves money. You decide.")
    with st.form(f"review_{d.request_id}"):
        reviewer = st.text_input("Your name")
        action = st.radio("What do you want to do?", REVIEW_ACTIONS)
        note = st.text_area("Notes (optional)", help="For example, why you chose this or any exception you allow.")
        if st.form_submit_button("Save my decision", type="primary"):
            if not reviewer.strip():
                st.error("Please type your name first.")
            else:
                REVIEW_LOG.parent.mkdir(exist_ok=True)
                entry = {"recorded_at": datetime.now(timezone.utc).isoformat(), "request_id": d.request_id,
                         "reviewer": reviewer.strip(), "decision": action, "note": note,
                         "copilot_recommendation": d.recommendation_type, "approvals": d.required_approvals,
                         "risk_flags": d.risk_flags}
                with REVIEW_LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
                st.success(f"Saved: \"{md(action)}\" by {md(reviewer.strip())}.")


# ---------------------------------------------------------------------------------------- sidebar
def new_request_form() -> dict | None:
    emp = employees()
    data_codes = list(DATA_ACCESS_TEXT)
    data_codes.remove("unknown")
    data_codes.insert(0, "unknown")
    with st.sidebar.form("new_request"):
        st.markdown("**Type a new request**")
        requester = st.selectbox("Who is asking?", list(emp),
                                 format_func=lambda e: f"{emp[e]['name']} ({emp[e]['department']})",
                                 help="The employee who wants the software.")
        product = st.text_input("What do they want to buy?", help="The product name, e.g. 'SignFlow Add-on'.")
        vendor = st.text_input("From which company (vendor)?", help="The company that sells it.")
        category = st.text_input("Type of software", help="For example: e-signature, design, note-taking.")
        cost = st.number_input("Cost per year in USD (0 = don't know)", min_value=0.0, step=100.0,
                               help="The total yearly price. Leave 0 if you don't know.")
        users = st.number_input("Number of people (0 = don't know)", min_value=0, step=1,
                                help="How many people will use it.")
        purpose = st.text_area("Why do they need it?", help="A short reason, in the requester's own words.")
        data = st.selectbox("What data will it touch?", data_codes, format_func=data_access_label,
                            help="The most sensitive company data the software will see.")
        integrations = st.text_input("Connects to (comma-separated)", help="Other systems it links to, e.g. SSO, Slack.")
        urgency = st.selectbox("Urgency", list(URGENCY_TEXT), format_func=URGENCY_TEXT.get)
        if st.form_submit_button("Use this request"):
            return {
                "request_id": f"NEW-{datetime.now().strftime('%H%M%S')}", "requester_id": requester,
                "product_name": product or None, "vendor_name": vendor or None, "category": category or None,
                "annual_cost_usd": cost or None, "user_count": int(users) or None, "business_justification": purpose,
                "data_access_level": data, "requested_integrations": [i.strip() for i in integrations.split(",") if i.strip()],
                "urgency": urgency,
            }
    return None


def apply_query_params(by_id: dict[str, dict]) -> None:
    """Deep links: ?request=REQ-1006&view=single|staged|compare&run=1&review=1 (applied once per session)."""
    if st.session_state.get("_qp_applied"):
        return
    st.session_state["_qp_applied"] = True
    qp = st.query_params
    rid = qp.get("request")
    if rid in by_id:
        st.session_state["source"] = SRC_EXISTING
        st.session_state["request_pick"] = rid
    view_label = {v: k for k, v in VIEW_OPTIONS.items()}.get(qp.get("view") or "")
    if view_label:
        st.session_state["view"] = view_label
    st.session_state["_autorun_pending"] = qp.get("run") == "1"


# ---------------------------------------------------------------------------------------- layout
by_id = requests_by_id()
apply_query_params(by_id)

st.title("Procurement Request Copilot")
st.markdown("Someone at the company wants to buy new software. This tool checks the facts and suggests "
            "what to do next. **A person always makes the final decision.**")
if llm_mode() == "llm":
    st.success(f"🟢 AI is on — using {provider()} / {model_name()}")
else:
    st.warning("⚪ AI is off — using company rules only. To turn the AI on, add a free API key "
               "(for example GEMINI_API_KEY) to the .env file and restart the app.")
st.caption(f"Company rules as of {reference_date()}")

HOW_IT_WORKS = ("**Pick a request** in the left panel.", "Click **Check this request**.",
                "Read the **advice** and the **reasons**.", "**You decide** what happens.")
st.markdown("**How it works**")
for num, (col, step_text) in enumerate(zip(st.columns(4), HOW_IT_WORKS), start=1):
    with col, st.container(border=True):
        st.markdown(f"**{num}** · {step_text}")

# sidebar
st.sidebar.header("1. Pick a request")
source = st.sidebar.radio("Where does the request come from?", [SRC_EXISTING, SRC_NEW], key="source")
if source == SRC_EXISTING:
    rid = st.sidebar.selectbox("Request", list(by_id), key="request_pick",
                               format_func=lambda r: f"{r} – {by_id[r]['product_name']}")
    req = by_id[rid]
else:
    submitted = new_request_form()
    if submitted:
        st.session_state["custom_request"] = submitted
    req = st.session_state.get("custom_request")

st.sidebar.header("2. How should the AI work?")
view_label = st.sidebar.radio("Choose one", list(VIEW_OPTIONS), key="view", help=VIEW_HELP)
st.sidebar.caption("One AI helper: one AI reads everything and gives advice.  \n"
                   "Two AI helpers: one AI gathers facts, a second writes the advice.  \n"
                   "Compare: runs both so you can see the difference.")
view = VIEW_OPTIONS[view_label]

st.divider()
if not req:
    st.info("Fill in the **Type a new request** form in the left panel, then click **Use this request**.")
    st.stop()

show_request(req)
st.divider()

st.header("Step 2 · Check the request")
archs = ["single", "staged"] if view == "compare" else [view]
cache_key = str((req["request_id"], tuple(archs)))
clicked = st.button("Check this request", type="primary", width="stretch")
autorun = st.session_state.pop("_autorun_pending", False) and cache_key not in st.session_state
if clicked or autorun:
    with st.spinner("Checking budget, existing tools, vendor safety and company rules… "
                    "With AI on this can take up to a minute."):
        st.session_state[cache_key] = {a: analyze(req, a) for a in archs}

st.divider()
st.header("Step 3 · What the copilot suggests")
results = st.session_state.get(cache_key)
if not results:
    st.info("Click **Check this request** to see the advice.")
    st.stop()

if len(results) == 1:
    show_decision(next(iter(results.values())), "one")
else:
    parts = []
    for arch, d in results.items():
        t = d.telemetry
        if t:
            parts.append(f"**{ARCH_TEXT.get(arch, arch)}:** {(t.latency_ms or 0) / 1000:.1f} s, {t.llm_calls or 0} AI calls")
    if parts:
        st.markdown("Comparison — " + " · ".join(parts))
    tabs = st.tabs([ARCH_TEXT["single"], ARCH_TEXT["staged"]])
    for tab, (arch, d) in zip(tabs, results.items()):
        with tab:
            show_decision(d, arch)

st.divider()
human_review(next(iter(results.values())))
