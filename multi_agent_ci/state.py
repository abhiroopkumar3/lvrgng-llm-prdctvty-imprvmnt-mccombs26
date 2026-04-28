# state.py
# Single source of truth for all inter-agent communication.
# No agent communicates through any channel other than these fields.

from typing import TypedDict


class State(TypedDict):
    company      : str   # User input — immutable once validated by UI Agent
    plan         : str   # Filled by Planner Agent (Agent 3)
    research_web : str   # Filled by Web Search Sub-Agent (Agent 4a)
    research_fin : str   # Filled by Financial Analyst Sub-Agent (Agent 4b)
    research     : str   # Merged by Synthesis Sub-Agent (Agent 4c)
    draft        : str   # Filled by Writer Agent (Agent 5)
    brief        : str   # Final validated output — promoted from draft by Supervisor
    next         : str   # Routing field — set ONLY by Supervisor
    retries      : int   # Global retry counter across full pipeline (max: MAX_RETRIES)
    hv_retries   : int   # Human validation sub-loop counter (max: MAX_HV_RETRIES)
    error        : str   # Last error message from any agent
    audit        : list  # List of dicts; structured log appended throughout pipeline


def fresh_state(company: str) -> State:
    """
    Create a clean initial State for a new pipeline run.
    Called by UI Agent (Intake) after company name is validated.
    """
    from datetime import datetime, timezone
    return State(
        company=company,
        plan="",
        research_web="",
        research_fin="",
        research="",
        draft="",
        brief="",
        next="supervisor",
        retries=0,
        hv_retries=0,
        error="",
        audit=[{
            "event": "pipeline_start",
            "agent": "ui_intake",
            "company": company,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "retries": 0,
        }],
    )
