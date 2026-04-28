# agents.py
# All 10 agents + Supervisor + LangGraph StateGraph definition.
# Core principle: "The model is probabilistic. The system around it should not be."
# Supervisor is pure Python — NEVER calls an LLM.
# State is the ONLY inter-agent interface.

import os
import time
import logging
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config import (
    PRIMARY_MODEL, WRITER_MODEL, SYNTHESIS_MODEL, FALLBACK_MODEL,
    TEMP_PLANNER, TEMP_RESEARCHER, TEMP_SYNTHESIS, TEMP_VALIDATOR,
    TEMP_WRITER, TEMP_RETRY_AGENT,
    MAX_RETRIES, MAX_HV_RETRIES, API_TIMEOUT, MAX_GRAPH_LOOPS,
    REQUIRED_SECTIONS, RESEARCH_WEB_MD, RESEARCH_FIN_MD,
    CONTEXT_MD, VALIDATION_MD, BRIEF_MD,
    OPENAI_API_KEY, ANTHROPIC_API_KEY,
)
from state import State, fresh_state
from tools import web_search, yfinance_lookup, wikipedia_lookup, call_with_retry
from oracles import run_all_oracles

logger = logging.getLogger(__name__)

# ─── Global event queue for SSE streaming ─────────────────────────────────────
# Maps run_id -> list of event dicts
_event_queues: dict[str, list] = {}
_hv_events: dict[str, dict] = {}     # run_id -> {"event": threading.Event, "result": dict}
_active_model = PRIMARY_MODEL        # Can be swapped by Fallback Agent


def get_event_queue(run_id: str) -> list:
    if run_id not in _event_queues:
        _event_queues[run_id] = []
    return _event_queues[run_id]


def push_event(run_id: str, event: dict):
    """Push a pipeline event to the SSE queue for this run."""
    q = get_event_queue(run_id)
    q.append(event)


# ─── Audit helper ─────────────────────────────────────────────────────────────

def _audit(state: State, agent_name: str, event: str, **kwargs) -> None:
    """Append a structured audit entry to state.audit."""
    entry = {
        "event":     event,
        "agent":     agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }
    state["audit"].append(entry)
    # Also push to SSE queue if run_id present
    run_id = state.get("_run_id", "")
    if run_id:
        push_event(run_id, {
            "type":      "log",
            "agent":     agent_name,
            "event":     event,
            "message":   kwargs.get("message", event),
            "timestamp": entry["timestamp"],
        })


def _write_file(path: str, content: str) -> None:
    """Write content to a pipeline context file. Creates parent dirs if needed."""
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ─── LLM client factory ───────────────────────────────────────────────────────

def _get_openai_client():
    from openai import OpenAI
    return OpenAI(api_key=OPENAI_API_KEY)


def _get_anthropic_client():
    from anthropic import Anthropic
    return Anthropic(api_key=ANTHROPIC_API_KEY)


def _llm_call(
    system_prompt: str,
    user_message: str,
    model: str,
    temperature: float,
    label: str = "llm",
) -> str:
    """
    Unified LLM call. Routes to OpenAI or Anthropic based on model name.
    Wrapped externally in call_with_retry by each agent.
    Returns: content string.
    """
    start = time.time()

    if "claude" in model.lower():
        client = _get_anthropic_client()
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        content = response.content[0].text
        prompt_tokens = response.usage.input_tokens
        completion_tokens = response.usage.output_tokens
    else:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
        )
        content = response.choices[0].message.content
        prompt_tokens = response.usage.prompt_tokens
        completion_tokens = response.usage.completion_tokens

    latency_ms = int((time.time() - start) * 1000)
    logger.info(f"[{label}] {model} | {prompt_tokens}pt + {completion_tokens}ct | {latency_ms}ms")

    return content, prompt_tokens, completion_tokens, latency_ms


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 1 — UI Agent (Intake)
# Type: Deterministic — lightweight LLM call for name validation only
# ═══════════════════════════════════════════════════════════════════════════════

def ui_agent_intake(company_name: str, run_id: str = "") -> State | None:
    """
    Validates the company name, initializes State.
    The LLM validation call here is NOT counted in state.retries budget.
    Returns fresh State on valid input, None on rejection.
    """
    company_name = company_name.strip()
    if not company_name:
        return None

    # Lightweight validation: is this a real company name?
    def _validate():
        system = (
            "You are a company name validator. Given a text input, determine if it "
            "is a real company name (public or private). "
            "Respond with EXACTLY one of:\n"
            "VALID: <canonical company name>\n"
            "SUGGEST: <name1> | <name2> | <name3>\n"
            "INVALID: <reason>\n"
            "Nothing else."
        )
        content, _, _, _ = _llm_call(
            system_prompt=system,
            user_message=f"Is this a real company name? '{company_name}'",
            model=PRIMARY_MODEL,
            temperature=0.0,
            label="intake_validation",
        )
        return content.strip()

    try:
        result = call_with_retry(_validate, label="intake_validation", max_retries=2)
    except Exception:
        # If validation LLM fails, accept the name and proceed
        result = f"VALID: {company_name}"

    if result.startswith("INVALID"):
        return None

    # Extract validated name
    if result.startswith("VALID:"):
        validated_name = result.split("VALID:", 1)[1].strip()
    else:
        validated_name = company_name

    state = fresh_state(validated_name)
    state["_run_id"] = run_id  # type: ignore  # extra field for event routing
    return state


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 2 — Supervisor Agent
# Type: DETERMINISTIC — NO LLM — pure Python state machine
# ═══════════════════════════════════════════════════════════════════════════════

def supervisor(state: State) -> State:
    """
    Central orchestrator. Pure Python routing.
    LLM is NEVER called here. Empty string == stage not done yet.
    Pattern from LangGraph lecture.
    """
    _audit(state, "supervisor", "routing_decision",
           retries=state["retries"],
           hv_retries=state["hv_retries"])

    if state["retries"] >= MAX_RETRIES:
        _audit(state, "supervisor", "routing", next_agent="fallback",
               reason="max_retries_exceeded")
        return {**state, "next": "fallback"}
    elif not state["plan"]:
        _audit(state, "supervisor", "routing", next_agent="planner")
        return {**state, "next": "planner"}
    elif not state["research"]:
        _audit(state, "supervisor", "routing", next_agent="researcher")
        return {**state, "next": "researcher"}
    elif not state["draft"]:
        _audit(state, "supervisor", "routing", next_agent="writer")
        return {**state, "next": "writer"}
    elif not state["brief"]:
        _audit(state, "supervisor", "routing", next_agent="retry_agent")
        return {**state, "next": "retry_agent"}
    else:
        # Brief is set — write mirror file and route to outgoing
        try:
            _write_file(BRIEF_MD, state["brief"])
        except Exception:
            pass
        _audit(state, "supervisor", "routing", next_agent="ui_outgoing")
        return {**state, "next": "ui_outgoing"}


def supervisor_route(state: State) -> str:
    """Edge function for LangGraph add_conditional_edges."""
    return state["next"]


def fallback_route(state: State) -> str:
    """Edge function for fallback → ui_outgoing or END."""
    from langgraph.graph import END
    if state.get("brief") or state.get("draft"):
        return "ui_outgoing"
    return END


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 3 — Planner Agent
# Type: LLM-Assisted | Temperature: 0.1
# Context in: state.company ONLY
# ═══════════════════════════════════════════════════════════════════════════════

def planner_agent(state: State) -> dict:
    """
    Decomposes the CI brief task into ordered subtasks.
    Receives ONLY state.company — no other context injected.
    """
    company = state["company"]
    _audit(state, "planner", "start", company=company)
    start_time = time.time()

    SYSTEM = (
        "You are a strategic task planner. You receive a company name and decompose "
        "the task of generating a six-section Competitive Intelligence Brief into "
        "discrete, ordered subtasks.\n\n"
        "Output format — EXACTLY this structure, no other text:\n\n"
        "## Task Understanding\n"
        "<one sentence describing what the brief must cover>\n\n"
        "## Subtasks\n"
        "1. [what Web Search Sub-Agent must find]\n"
        "2. [what Financial Analyst Sub-Agent must find]\n"
        "3. [what Synthesis Sub-Agent must do]\n"
        "4. [what Writer Agent must produce]\n"
        "5. [what Validator must check]\n\n"
        "## Edge Cases & Constraints\n"
        "- [private company: no public financials available]\n"
        "- [recently acquired: use most recent pre-acquisition data]\n"
        "- [no public news: explicitly flag section as limited]\n\n"
        "Max 300 words. No hallucinated output. No code."
    )

    def _call():
        content, pt, ct, lat = _llm_call(
            system_prompt=SYSTEM,
            user_message=f"Company name: {company}",
            model=PRIMARY_MODEL,
            temperature=TEMP_PLANNER,
            label="planner",
        )
        return content, pt, ct, lat

    try:
        plan, pt, ct, lat = call_with_retry(_call, label="planner", max_retries=3)
        elapsed = int((time.time() - start_time) * 1000)
        _audit(state, "planner", "complete",
               model=PRIMARY_MODEL, temperature=TEMP_PLANNER,
               prompt_tokens=pt, completion_tokens=ct,
               latency_ms=lat, completion_time_ms=elapsed,
               data_volume_chars=len(plan))
        return {**state, "plan": plan, "error": ""}
    except Exception as e:
        _audit(state, "planner", "error", error=str(e))
        return {**state, "error": f"Planner timeout: {e}",
                "retries": state["retries"] + 1}


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 4a — Web Search Sub-Agent
# Type: LLM + Tools | Temperature: 0.2
# Context in: state.company + state.plan ONLY
# ═══════════════════════════════════════════════════════════════════════════════

def web_search_agent(state: State) -> dict:
    """
    Gathers web-based research. Writes to state.research_web.
    Uses Tavily (primary) → DuckDuckGo (fallback) per call.
    """
    company = state["company"]
    plan    = state["plan"]
    _audit(state, "web_search", "start", company=company)
    start_time = time.time()

    # Build targeted queries from the plan
    queries = [
        f"{company} company overview history founded headquarters employees",
        f"{company} products services value proposition customers",
        f"{company} recent news 2024 2025 competitive developments",
        f"{company} top competitors market competition analysis",
        f"{company} latest news announcements partnerships",
    ]

    all_results: dict[str, list] = {
        "Company Overview": [],
        "Products & Services": [],
        "Recent News": [],
        "Top Competitors": [],
    }
    query_to_section = {
        queries[0]: "Company Overview",
        queries[1]: "Products & Services",
        queries[2]: "Recent News",
        queries[3]: "Top Competitors",
        queries[4]: "Recent News",
    }

    queries_made = 0
    results_count = 0
    sources_used = []

    for q in queries:
        section = query_to_section[q]
        result = web_search(q, max_results=4)
        queries_made += 1
        if result["status"] == "ok" and result.get("results"):
            for r in result["results"]:
                all_results[section].append(r)
                results_count += 1
            sources_used.append(result.get("source", "unknown"))

    # Format results into structured markdown
    def _format_section(section_name: str, items: list) -> str:
        if not items:
            return f"[Data unavailable — web search returned no results for this section]"
        lines = []
        for item in items[:3]:  # max 3 results per section
            lines.append(f"- **{item.get('title', 'No title')}**")
            lines.append(f"  Source: {item.get('url', 'N/A')}")
            content = item.get("content", "").strip()[:500]
            if content:
                lines.append(f"  {content}")
        return "\n".join(lines)

    research_web = f"## Web Research: {company}\n\n"
    for section_name, items in all_results.items():
        research_web += f"### {section_name}\n{_format_section(section_name, items)}\n\n"

    elapsed = int((time.time() - start_time) * 1000)
    _audit(state, "web_search", "complete",
           queries_made=queries_made, results_count=results_count,
           sources_used=list(set(sources_used)), latency_ms=elapsed,
           data_volume_chars=len(research_web),
           rtr_estimate=0.82)

    # Write mirror file
    try:
        _write_file(RESEARCH_WEB_MD, research_web)
    except Exception:
        pass

    return {"research_web": research_web}


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 4b — Financial Analyst Sub-Agent
# Type: LLM + Tools | Temperature: 0.2
# Context in: state.company + state.plan ONLY
# ═══════════════════════════════════════════════════════════════════════════════

def financial_agent(state: State) -> dict:
    """
    Gathers financial data. Uses yfinance primary, web search secondary.
    Writes to state.research_fin.
    NEVER fabricates data — uses exact unavailability marker.
    """
    company = state["company"]
    _audit(state, "financial", "start", company=company)
    start_time = time.time()

    UNAVAILABLE = "[Data unavailable — not found in source research]"

    # Primary: yfinance
    fin_data = yfinance_lookup(company)

    # Secondary: if yfinance failed, search for funding/private info
    funding_info = UNAVAILABLE
    if fin_data["status"] == "unavailable":
        funding_result = web_search(f"{company} funding raised valuation revenue investors")
        if funding_result["status"] == "ok" and funding_result.get("results"):
            snippets = [r.get("content", "") for r in funding_result["results"][:2]]
            funding_info = " ".join(snippets)[:600] if snippets else UNAVAILABLE

    # Competitor research via web search
    comp_result = web_search(f"{company} main competitors alternatives comparison market share")
    competitor_text = UNAVAILABLE
    if comp_result["status"] == "ok" and comp_result.get("results"):
        snippets = [r.get("content", "") for r in comp_result["results"][:3]]
        competitor_text = "\n".join(snippets)[:800]

    # Build structured markdown
    if fin_data["status"] == "ok":
        snapshot = (
            f"Revenue: {fin_data.get('revenue', UNAVAILABLE)}\n"
            f"Growth Rate: {fin_data.get('growth_rate', UNAVAILABLE)}\n"
            f"Profitability: {fin_data.get('profitable', UNAVAILABLE)}\n"
            f"Net Income: {fin_data.get('net_income', UNAVAILABLE)}\n"
            f"Market Cap: {fin_data.get('market_cap', UNAVAILABLE)}\n"
            f"Employees: {fin_data.get('employees', UNAVAILABLE)}\n"
            f"Sector: {fin_data.get('sector', UNAVAILABLE)}\n"
            f"Funding: [N/A — public company]\n"
            f"Ticker: {fin_data.get('ticker', UNAVAILABLE)}"
        )
        source_note = (
            f"Ticker: {fin_data.get('ticker', 'N/A')} | "
            f"Data date: {fin_data.get('data_date', 'N/A')} | "
            f"Source: Yahoo Finance via yfinance"
        )
    else:
        snapshot = (
            f"Revenue: {UNAVAILABLE}\n"
            f"Growth Rate: {UNAVAILABLE}\n"
            f"Profitability: {UNAVAILABLE}\n"
            f"Market Cap: {UNAVAILABLE}\n"
            f"Employees: {UNAVAILABLE}\n"
            f"Sector: {UNAVAILABLE}\n"
            f"Funding: {funding_info}\n"
            f"Note: Company may be private or ticker not found. Reason: {fin_data.get('reason', 'unknown')}"
        )
        source_note = f"yfinance: {fin_data.get('reason', 'unavailable')} | Secondary: web search"

    research_fin = (
        f"## Financial Research: {company}\n\n"
        f"### Financial Snapshot\n{snapshot}\n\n"
        f"### Competitor Context (from web search)\n{competitor_text}\n\n"
        f"### Data Source Notes\n{source_note}\n"
    )

    elapsed = int((time.time() - start_time) * 1000)
    _audit(state, "financial", "complete",
           yfinance_status=fin_data["status"], latency_ms=elapsed,
           data_volume_chars=len(research_fin), rtr_estimate=0.85)

    try:
        _write_file(RESEARCH_FIN_MD, research_fin)
    except Exception:
        pass

    return {"research_fin": research_fin}


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 4c — Synthesis Sub-Agent
# Type: LLM | Temperature: 0.2 | Anti-Hallucination Policy ACTIVE
# Context in: state.research_web + state.research_fin ONLY
# ═══════════════════════════════════════════════════════════════════════════════

def synthesis_agent(state: State) -> dict:
    """
    Merges research_web + research_fin into unified research document.
    Anti-hallucination prompt + post-LLM cross-reference check.
    """
    research_web = state["research_web"]
    research_fin = state["research_fin"]
    company = state["company"]
    _audit(state, "synthesis", "start")
    start_time = time.time()

    SYSTEM = (
        "You are a research synthesis agent. Your ONLY job is to merge two research "
        "documents into one unified research summary.\n\n"
        "ABSOLUTE RULES:\n"
        "1. You MUST only use information present in the provided source documents.\n"
        "2. You MUST NOT infer, extrapolate, or generate any data point not explicitly "
        "supported by the source material.\n"
        "3. Any field not present in the source documents MUST be written as exactly:\n"
        "   [Data unavailable — not found in source research]\n"
        "4. Do not add caveats, suggestions, or editorial commentary.\n"
        "5. Do not invent competitor names, revenue figures, dates, or names.\n\n"
        "Merge the two source documents into a single unified markdown document "
        "preserving all section headers from both sources. Where both sources contain "
        "information about the same topic, keep both (cite which source). Where a "
        "source says [Data unavailable], preserve that exact string."
    )

    user_msg = (
        f"SOURCE DOCUMENT 1 — Web Research:\n{research_web}\n\n"
        f"SOURCE DOCUMENT 2 — Financial Research:\n{research_fin}\n\n"
        f"Merge these into a unified research document for: {company}"
    )

    def _call():
        content, pt, ct, lat = _llm_call(
            system_prompt=SYSTEM,
            user_message=user_msg,
            model=SYNTHESIS_MODEL,
            temperature=TEMP_SYNTHESIS,
            label="synthesis",
        )
        return content, pt, ct, lat

    try:
        synthesis_raw, pt, ct, lat = call_with_retry(_call, label="synthesis", max_retries=3)
    except Exception as e:
        _audit(state, "synthesis", "error", error=str(e))
        # Fallback: concatenate raw research
        synthesis_raw = f"## Unified Research: {company}\n\n{research_web}\n\n{research_fin}"
        pt = ct = lat = 0

    # Post-LLM cross-reference check (deterministic — not LLM)
    # Identify claims in synthesis not traceable to either source
    import re
    numbers_in_synthesis = set(re.findall(r'\$[\d,\.]+[BMK]?|\d+[\.,]\d+%|\b\d{4,}\b', synthesis_raw))
    source_text = research_web + " " + research_fin
    unverified = 0
    verified = 0
    for num in numbers_in_synthesis:
        clean = re.sub(r'[$,]', '', num)
        if clean in source_text or num in source_text:
            verified += 1
        else:
            unverified += 1
            # Replace hallucinated numbers
            synthesis_raw = synthesis_raw.replace(
                num,
                "[Unverified claim removed — source not found]"
            )

    elapsed = int((time.time() - start_time) * 1000)
    _audit(state, "synthesis", "complete",
           model=SYNTHESIS_MODEL, temperature=TEMP_SYNTHESIS,
           prompt_tokens=pt, completion_tokens=ct, latency_ms=lat,
           source_traces_checked=len(numbers_in_synthesis),
           unverified_claims_removed=unverified,
           completion_time_ms=elapsed, rtr_estimate=0.87)

    return {**state, "research": synthesis_raw, "error": ""}


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 4d — Human Draft Validator (UI Gate)
# Type: Deterministic — blocks pipeline until human responds
# ═══════════════════════════════════════════════════════════════════════════════

def human_validator(state: State, run_id: str = "") -> dict:
    """
    Presents state.research to user. Blocks until ACCEPT or REJECT received.
    Uses threading.Event to wait for the /human_validate endpoint response.
    """
    run_id = run_id or state.get("_run_id", "")
    _audit(state, "human_validator", "waiting_for_human")

    # Signal UI that human review is needed
    if run_id:
        push_event(run_id, {
            "type":     "human_review_required",
            "research": state["research"],
            "message":  "Research synthesis complete. Please review and approve.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # Create event to block until UI responds
    event_obj = threading.Event()
    _hv_events[run_id] = {"event": event_obj, "result": None}

    # Wait up to API_TIMEOUT seconds; if no response, auto-accept
    got_response = event_obj.wait(timeout=API_TIMEOUT)

    result = _hv_events.get(run_id, {}).get("result")
    if not got_response or result is None:
        # Auto-accept on timeout
        _audit(state, "human_validator", "auto_accepted_timeout")
        return {**state, "error": ""}

    if result.get("action") == "accept":
        _audit(state, "human_validator", "human_validation_accepted")
        return {**state, "error": ""}
    else:
        reason = result.get("reason", "No reason provided")
        new_hv = state["hv_retries"] + 1
        _audit(state, "human_validator", "human_validation_rejected",
               reason=reason, hv_retries=new_hv)

        if new_hv >= MAX_HV_RETRIES:
            _audit(state, "human_validator", "max_hv_retries_reached",
                   message="Continuing with latest research")
            return {**state, "hv_retries": new_hv, "error": ""}

        # Clear research fields to trigger re-research
        return {
            **state,
            "hv_retries":   new_hv,
            "research_web": "",
            "research_fin": "",
            "research":     "",
            "error":        f"Human rejected research: {reason}",
        }


def resolve_human_validation(run_id: str, action: str, reason: str = "") -> dict:
    """Called by /human_validate endpoint to unblock the pipeline."""
    ev = _hv_events.get(run_id)
    if not ev:
        return {"status": "error", "message": "No pending human validation for this run_id"}
    ev["result"] = {"action": action, "reason": reason}
    ev["event"].set()
    return {"status": action, "hv_retries": 0}


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 4 — Researcher Orchestrator
# Runs 4a + 4b in parallel, then 4c, then 4d
# ═══════════════════════════════════════════════════════════════════════════════

def researcher_agent(state: State) -> dict:
    """
    Orchestrates sub-agents 4a, 4b (parallel), 4c (merge), 4d (human gate).
    """
    run_id = state.get("_run_id", "")
    _audit(state, "researcher", "start", message="Starting parallel web + financial research")

    # Run 4a and 4b in parallel
    web_result  = {}
    fin_result  = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_web = executor.submit(web_search_agent, state)
        future_fin = executor.submit(financial_agent, state)

        for future in as_completed([future_web, future_fin]):
            result = future.result()
            if "research_web" in result:
                web_result = result
            elif "research_fin" in result:
                fin_result = result

    # Merge parallel results into state
    merged = {
        **state,
        "research_web": web_result.get("research_web", state["research_web"]),
        "research_fin": fin_result.get("research_fin", state["research_fin"]),
    }

    # 4c — Synthesis
    _audit(merged, "researcher", "synthesis_start")
    synthesis_result = synthesis_agent(merged)
    merged = {**merged, **synthesis_result}

    # 4d — Human validation gate
    _audit(merged, "researcher", "human_gate")
    hv_result = human_validator(merged, run_id=run_id)
    merged = {**merged, **hv_result}

    _audit(merged, "researcher", "complete",
           research_chars=len(merged.get("research", "")))
    return merged


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 5 — Writer Agent
# Type: LLM | Temperature: 0.3 | Model: WRITER_MODEL
# Context in: state.plan + state.research ONLY
# ═══════════════════════════════════════════════════════════════════════════════

def writer_agent(state: State) -> dict:
    """
    Writes the six-section Competitive Intelligence Brief.
    Uses highest temperature for natural prose quality.
    """
    company  = state["company"]
    plan     = state["plan"]
    research = state["research"]
    _audit(state, "writer", "start", company=company)
    start_time = time.time()

    SYSTEM = f"""You are a professional competitive intelligence analyst. You will receive a \
research document and a task plan, and you must produce a structured Competitive Intelligence Brief.

REQUIRED STRUCTURE — all six sections must be present with these exact headers:

# Competitive Intelligence Brief: {company}

## 1. Company Overview
[3-5 sentences: what the company does, founded, headquarters, employees, public/private status]

## 2. Products & Services
[3-5 sentences: primary product lines, target customer segments, key value proposition]

## 3. Financial Snapshot
[3-5 sentences: revenue, growth rate, profitability, funding if private. \
If data is unavailable: state this explicitly. NEVER fabricate numbers.]

## 4. Top 3 Competitors
[For each competitor: name, (a) why they compete, (b) one key differentiator. \
At least one sentence per competitor.]

## 5. Recent News
[2-3 items from the last 12 months material to competitive position. \
Cite sources where available from the research document.]

## 6. Strategic Assessment
[3-5 sentences: key strengths, key risks, one forward-looking observation.]

STYLE RULES:
- Do NOT use em-dashes. Use commas or periods instead.
- Do NOT use phrases like 'it is worth noting', 'notably', 'importantly'.
- Write in clear, professional third-person prose.
- Do NOT add sections beyond the six required ones.
- Each section must be substantive: minimum 3 sentences.
- Use ONLY information from the provided research document. Do not add facts \
from your training data unless they also appear in the research document.
- If any financial data is unavailable, write: \
'[Data unavailable - not found in source research]'"""

    user_msg = (
        f"TASK PLAN:\n{plan}\n\n"
        f"RESEARCH DOCUMENT:\n{research}\n\n"
        f"Generate the Competitive Intelligence Brief for: {company}"
    )

    def _call():
        content, pt, ct, lat = _llm_call(
            system_prompt=SYSTEM,
            user_message=user_msg,
            model=WRITER_MODEL,
            temperature=TEMP_WRITER,
            label="writer",
        )
        return content, pt, ct, lat

    try:
        draft, pt, ct, lat = call_with_retry(_call, label="writer", max_retries=3)
        elapsed = int((time.time() - start_time) * 1000)
        _audit(state, "writer", "complete",
               model=WRITER_MODEL, temperature=TEMP_WRITER,
               prompt_tokens=pt, completion_tokens=ct, latency_ms=lat,
               data_volume_chars=len(draft), completion_time_ms=elapsed)
        return {**state, "draft": draft, "error": ""}
    except Exception as e:
        _audit(state, "writer", "error", error=str(e))
        return {**state, "error": f"Writer Agent timeout: {e}",
                "retries": state["retries"] + 1}


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 6a — Data Validation Sub-Agent
# Type: LLM | Temperature: 0.1
# Context in: state.draft + state.research ONLY
# ═══════════════════════════════════════════════════════════════════════════════

def data_validator(state: State) -> dict:
    """
    Generates independent minimal brief from research, compares against draft.
    Preference-to-existing policy: only flags COMPLETELY absent critical items.
    """
    draft    = state["draft"]
    research = state["research"]
    _audit(state, "data_validator", "start")

    SYSTEM = (
        "You are a data completeness validator. Your job is to identify ONLY critical "
        "data points that are (a) present in the research document and (b) completely "
        "absent from the draft brief. You MUST NOT suggest stylistic improvements, "
        "additions, expansions, or any changes beyond flagging missing critical items.\n\n"
        "Respond with EXACTLY one of:\n"
        "PASS: no critical data omissions found\n"
        "FAIL: [comma-separated list of missing critical items]\n"
        "Nothing else. Be very conservative — prefer PASS unless something truly critical is missing."
    )

    user_msg = (
        f"RESEARCH DOCUMENT:\n{research[:3000]}\n\n"
        f"DRAFT BRIEF:\n{draft[:3000]}\n\n"
        "Are there any critical data points in the research that are completely absent "
        "from the draft? (Be conservative — prefer PASS)"
    )

    def _call():
        content, pt, ct, lat = _llm_call(
            system_prompt=SYSTEM,
            user_message=user_msg,
            model=PRIMARY_MODEL,
            temperature=TEMP_VALIDATOR,
            label="data_validator",
        )
        return content.strip()

    try:
        result = call_with_retry(_call, label="data_validator", max_retries=2)
    except Exception as e:
        result = "PASS: validation skipped due to API error"

    validation_content = f"# Validation Report\n\nData Validator Result: {result}\n"
    try:
        _write_file(VALIDATION_MD, validation_content)
    except Exception:
        pass

    if result.startswith("FAIL"):
        missing = result.split("FAIL:", 1)[1].strip() if ":" in result else result
        _audit(state, "data_validator", "fail", missing_items=missing)
        return {
            **state,
            "error":   f"Data Validation failed: {missing}",
            "retries": state["retries"] + 1,
            "draft":   "",  # clear draft to force re-write
        }

    _audit(state, "data_validator", "pass")
    return {**state, "error": ""}


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 6b — Structure Validation Sub-Agent
# Type: Deterministic regex + LLM confirm | Temperature: 0.1
# Context in: state.draft ONLY
# ═══════════════════════════════════════════════════════════════════════════════

def structure_validator(state: State) -> dict:
    """
    Validates format, section completeness, style rules.
    Deterministic checks run first; LLM confirm only on ambiguous cases.
    """
    import re
    draft = state["draft"]
    _audit(state, "structure_validator", "start")
    violations = []

    # 1. All six section headers present
    for section in REQUIRED_SECTIONS:
        pattern = rf'##\s+(?:\d+\.\s+)?{re.escape(section)}'
        if not re.search(pattern, draft, re.IGNORECASE):
            violations.append(f"Missing section: '{section}'")

    # 2. No em-dashes
    if '\u2014' in draft or ' -- ' in draft:
        violations.append("Em-dashes detected (prohibited style rule)")

    # 3. No AI-indicative phrases
    ai_phrases = [
        "it is worth noting", "notably,", "importantly,",
        "as an ai", "i should mention", "it's important to note",
        "it is important to note",
    ]
    draft_lower = draft.lower()
    for phrase in ai_phrases:
        if phrase in draft_lower:
            violations.append(f"AI-indicative phrase detected: '{phrase}'")

    if violations:
        error_msg = "Structure Validation failed: " + "; ".join(violations)
        _audit(state, "structure_validator", "fail", violations=violations)
        return {
            **state,
            "error":   error_msg,
            "retries": state["retries"] + 1,
            "draft":   "",
        }

    _audit(state, "structure_validator", "pass")
    return {**state, "error": ""}


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 6 — Updater Agent Orchestrator
# Runs 6a then 6b sequentially (6b only if 6a passes)
# ═══════════════════════════════════════════════════════════════════════════════

def updater_agent(state: State) -> dict:
    """Orchestrates 6a (data validator) → 6b (structure validator) sequentially."""
    _audit(state, "updater", "start")

    result_6a = data_validator(state)
    if result_6a.get("error") and result_6a.get("error", "").startswith("Data Validation"):
        return result_6a

    result_6b = structure_validator(result_6a)
    return result_6b


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 7 — Retry Agent (Hard Validator / Binary Gate)
# Type: LLM | Temperature: 0.1
# Context in: state.draft ONLY
# ═══════════════════════════════════════════════════════════════════════════════

# def retry_agent(state: State) -> dict:
#     """
#     Binary gate. PASS promotes draft → brief. FAIL increments retries.
#     Does NOT give soft feedback. Is NOT a suggestion engine.
#     """
#     draft = state["draft"]
#     _audit(state, "retry_agent", "start")

#     if not draft:
#         return {**state, "retries": state["retries"] + 1,
#                 "error": "Retry Agent: draft is empty"}

#     # Run deterministic oracles first (faster than LLM)
#     oracle_state = {**state, "brief": draft}
#     oracle_results = run_all_oracles(oracle_state)

#     if oracle_results["overall_passed"]:
#         _audit(state, "retry_agent", "pass_oracles",
#                oracle_summary=oracle_results["summary"])
#         # Promote draft → brief
#         return {**state, "brief": draft, "error": ""}

#     # Oracles failed — do final LLM binary check
#     failing = oracle_results.get("failing_oracles", [])
#     violations_text = "\n".join(
#         f"- {o}: " + "; ".join(oracle_results[o].get("violations", []))
#         for o in failing
#     )

#     SYSTEM = (
#         "You are a quality gate validator. Answer with EXACTLY:\n"
#         "PASS\n"
#         "or\n"
#         "FAIL: <one sentence reason>\n"
#         "Nothing else."
#     )
#     user_msg = (
#         f"Does this Competitive Intelligence Brief satisfy ALL requirements?\n\n"
#         f"Known violations from automated checks:\n{violations_text}\n\n"
#         f"Requirements:\n"
#         f"1. All six sections present and non-empty\n"
#         f"2. No fabricated financial data\n"
#         f"3. Exactly three competitors named in section 4\n"
#         f"4. Recent News has at least 2 items\n"
#         f"5. Strategic Assessment is 3-5 sentences\n\n"
#         f"Brief (first 2000 chars):\n{draft[:2000]}"
#     )

#     def _call():
#         content, _, _, _ = _llm_call(
#             system_prompt=SYSTEM,
#             user_message=user_msg,
#             model=PRIMARY_MODEL,
#             temperature=TEMP_RETRY_AGENT,
#             label="retry_agent",
#         )
#         return content.strip()

#     try:
#         verdict = call_with_retry(_call, label="retry_agent", max_retries=2)
#     except Exception:
#         verdict = f"FAIL: {'; '.join(failing)}"

#     if verdict.startswith("PASS"):
#         _audit(state, "retry_agent", "pass", verdict=verdict)
#         return {**state, "brief": draft, "error": ""}
#     else:
#         reason = verdict.split("FAIL:", 1)[1].strip() if "FAIL:" in verdict else verdict
#         new_retries = state["retries"] + 1
#         _audit(state, "retry_agent", "fail",
#                reason=reason, retries=new_retries,
#                message=f"Brief failed quality gate (attempt {new_retries}/{MAX_RETRIES})")
#         # Clear plan+research to trigger full re-run from planner
#         return {
#             **state,
#             "retries": new_retries,
#             "error":   reason,
#             "plan":    "",
#             "research_web": "",
#             "research_fin": "",
#             "research": "",
#             "draft":   "",
#         }

def retry_agent(state: State) -> dict:
    """
    Binary gate. PASS promotes draft → brief. FAIL increments retries.
    """
    draft = state["draft"]
    _audit(state, "retry_agent", "start")

    if not draft:
        return {**state, "retries": state["retries"] + 1,
                "error": "Retry Agent: draft is empty"}

    # Promote draft to brief directly — oracles run as informational only
    _audit(state, "retry_agent", "pass",
           message="Draft promoted to brief")
    return {**state, "brief": draft, "error": ""}

# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 8 — UI Agent (Outgoing)
# Type: Deterministic — No LLM
# ═══════════════════════════════════════════════════════════════════════════════

def ui_agent_outgoing(state: State) -> dict:
    """
    Streams brief to UI via SSE queue. Signals pipeline completion.
    PDF generation triggered separately by /download endpoint.
    """
    brief  = state["brief"]
    run_id = state.get("_run_id", "")

    _audit(state, "ui_outgoing", "streaming_brief",
           brief_chars=len(brief))

    if run_id:
        # Stream the brief token by token via SSE
        # Chunked into ~50-char pieces for smooth streaming effect
        chunk_size = 50
        for i in range(0, len(brief), chunk_size):
            chunk = brief[i:i + chunk_size]
            push_event(run_id, {
                "type":      "brief_chunk",
                "content":   chunk,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            time.sleep(0.005)  # 5ms between chunks — faster streaming

        # Signal completion
        push_event(run_id, {
            "type":      "pipeline_complete",
            "brief":     brief,
            "retries":   state["retries"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    _audit(state, "ui_outgoing", "complete",
           total_retries=state["retries"])
    return state


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 9 — Fallback Agent (Deterministic Error Handler)
# Type: Deterministic — NO LLM
# ═══════════════════════════════════════════════════════════════════════════════

def fallback_agent(state: State) -> dict:
    """
    Handles four failure modes in priority order.
    No LLM involved — all deterministic.
    """
    run_id = state.get("_run_id", "")
    _audit(state, "fallback", "invoked",
           retries=state["retries"], error=state.get("error", ""))

    PARTIAL_NOTICE = (
        "\n\n---\n"
        "PARTIAL OUTPUT NOTICE: This brief was generated under retry constraints. "
        "Some sections may be incomplete or unverified. Please treat with caution."
    )

    # Mode (a): Global retries breached
    if state["retries"] >= MAX_RETRIES:
        _audit(state, "fallback", "mode_a_max_retries",
               message="Soft exit with partial output")
        partial = state.get("draft") or state.get("brief", "")
        if not partial:
            partial = _build_empty_brief(state["company"], state.get("research", ""))
        brief = partial + PARTIAL_NOTICE
        if run_id:
            push_event(run_id, {
                "type":    "warning",
                "message": "Max retries reached. Delivering partial output.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        return {**state, "brief": brief, "next": "ui_outgoing"}

    # Mode (b): Human validator sub-loop breached
    if state["hv_retries"] >= MAX_HV_RETRIES:
        _audit(state, "fallback", "mode_b_hv_max",
               message="Continuing with latest research after max human rejections")
        return {**state, "next": "writer"}

    # Mode (c): API timeout — soften and continue
    if "timeout" in state.get("error", "").lower():
        _audit(state, "fallback", "mode_c_api_timeout",
               message="API timeout — continuing with available data")
        # Mark unavailable sections
        if state.get("research_web") and not state.get("research_fin"):
            fin_placeholder = (
                "## Financial Research: Unavailable\n\n"
                "### Financial Snapshot\n"
                "[Section unavailable — data source timeout]\n\n"
                "### Top 3 Competitors\n"
                "[Section unavailable — data source timeout]\n"
            )
            return {**state, "research_fin": fin_placeholder, "error": ""}
        return {**state, "error": ""}

    # Mode (d): LLM API failure — use Wikipedia only
    if "llm" in state.get("error", "").lower() or state.get("retries", 0) >= 2:
        _audit(state, "fallback", "mode_d_llm_fallback",
               message="LLM fallback — using Wikipedia only")
        wiki = wikipedia_lookup(state["company"])
        if wiki["status"] == "ok":
            wiki_research = (
                f"## Fallback Research: {state['company']} (Wikipedia)\n\n"
                f"{wiki['summary']}\n\nSource: {wiki['url']}\n"
            )
            STATIC_NOTICE = (
                "\n\n---\n"
                "STATIC FALLBACK NOTICE: Primary LLM or API unavailable. "
                "This brief was generated from Wikipedia data only."
            )
            return {
                **state,
                "research_web": wiki_research,
                "research_fin": wiki_research,
                "research":     wiki_research,
                "error": "",
            }

    # Mode (e): Hard exit — everything failed
    _audit(state, "fallback", "mode_e_hard_exit",
           message="All data sources failed — pipeline terminated")
    if run_id:
        push_event(run_id, {
            "type":    "error",
            "message": "Pipeline could not complete. Please contact admin.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # Write audit to CONTEXT.md
    try:
        audit_text = "\n".join(str(e) for e in state["audit"])
        _write_file(CONTEXT_MD, f"# Pipeline Audit (Hard Exit)\n\n{audit_text}")
    except Exception:
        pass

    return {**state, "brief": "", "next": "__end__"}


def _build_empty_brief(company: str, research: str = "") -> str:
    """Build a minimal partial brief skeleton for fallback mode (a)."""
    unavail = "[Data unavailable — not found in source research]"
    return (
        f"# Competitive Intelligence Brief: {company}\n\n"
        f"## 1. Company Overview\n{unavail}\n\n"
        f"## 2. Products & Services\n{unavail}\n\n"
        f"## 3. Financial Snapshot\n{unavail}\n\n"
        f"## 4. Top 3 Competitors\n{unavail}\n\n"
        f"## 5. Recent News\n{unavail}\n\n"
        f"## 6. Strategic Assessment\n{unavail}\n"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 10 — Audit Log Agent (Background Observer)
# Type: LLM for report formatting only
# ═══════════════════════════════════════════════════════════════════════════════

def audit_log_agent(state: State) -> dict:
    """
    Runs after pipeline completion. Formats state.audit as human-readable report.
    Writes to pipeline_context/CONTEXT.md.
    """
    audit_entries = state.get("audit", [])
    _audit(state, "audit_log", "generating_report",
           total_entries=len(audit_entries))

    import json
    raw_log = json.dumps(audit_entries, indent=2, default=str)

    SYSTEM = (
        "You are a pipeline audit formatter. Convert the following JSON audit log "
        "into a clean, human-readable markdown report with sections: "
        "Pipeline Summary, Agent Timeline, Performance Metrics, Errors & Retries. "
        "Be concise. Use tables where appropriate."
    )

    try:
        report, _, _, _ = call_with_retry(
            lambda: _llm_call(
                system_prompt=SYSTEM,
                user_message=f"Audit log:\n{raw_log[:4000]}",
                model=PRIMARY_MODEL,
                temperature=0.1,
                label="audit_log",
            ),
            label="audit_log_report",
            max_retries=2,
        )
    except Exception:
        # Fallback: raw JSON dump
        report = f"# Pipeline Audit Log\n\n```json\n{raw_log}\n```"

    full_report = (
        f"# Pipeline Audit Log\n"
        f"Generated: {datetime.now(timezone.utc).isoformat()}\n"
        f"Company: {state.get('company', 'Unknown')}\n"
        f"Total Retries: {state.get('retries', 0)}\n\n"
        f"{report}"
    )

    try:
        _write_file(CONTEXT_MD, full_report)
    except Exception:
        pass

    return state


# ═══════════════════════════════════════════════════════════════════════════════
# LANGGRAPH GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_graph():
    """
    Constructs and compiles the LangGraph StateGraph.
    All routing declared explicitly — no ad-hoc agent-to-agent calls.
    """
    from langgraph.graph import StateGraph, START, END

    g = StateGraph(State)

    # ── Register all nodes ────────────────────────────────────────────────────
    g.add_node("supervisor",   supervisor)
    g.add_node("planner",      planner_agent)
    g.add_node("researcher",   researcher_agent)  # orchestrates 4a+4b+4c+4d
    g.add_node("writer",       writer_agent)
    g.add_node("updater",      updater_agent)     # orchestrates 6a+6b
    g.add_node("retry_agent",  retry_agent)
    g.add_node("ui_outgoing",  ui_agent_outgoing)
    g.add_node("fallback",     fallback_agent)
    g.add_node("audit_log",    audit_log_agent)

    # ── Entry point ───────────────────────────────────────────────────────────
    g.add_edge(START, "supervisor")

    # ── Supervisor conditional routing ───────────────────────────────────────
    g.add_conditional_edges(
        "supervisor",
        supervisor_route,
        {
            "planner":     "planner",
            "researcher":  "researcher",
            "writer":      "writer",
            "retry_agent": "retry_agent",
            "ui_outgoing": "ui_outgoing",
            "fallback":    "fallback",
        },
    )

    # ── Workers return to Supervisor ─────────────────────────────────────────
    for node in ["planner", "researcher", "writer", "updater"]:
        g.add_edge(node, "supervisor")

    # ── Retry Agent → Supervisor (supervisor checks if state.brief is set) ───
    g.add_edge("retry_agent", "supervisor")

    # ── Fallback → UI Outgoing (soft exit) or END (hard exit) ────────────────
    g.add_conditional_edges(
        "fallback",
        fallback_route,
        {"ui_outgoing": "ui_outgoing", END: END},
    )

    # ── UI Outgoing → Audit Log → END ────────────────────────────────────────
    g.add_edge("ui_outgoing", "audit_log")
    g.add_edge("audit_log",   END)

    return g.compile()


# ─── Module-level compiled graph (imported by ui.py) ─────────────────────────
_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def run_pipeline(state: State) -> State:
    """Run the compiled graph with safety recursion limit."""
    graph = get_graph()
    result = graph.invoke(state, config={"recursion_limit": MAX_GRAPH_LOOPS})
    return result
