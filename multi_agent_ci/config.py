# config.py
# All tunable constants. Nothing is hardcoded anywhere else in the project.
# Import everything from here: from config import *

import os
from dotenv import load_dotenv

load_dotenv()

# ─── LLM Models ────────────────────────────────────────────────────────────────
PRIMARY_MODEL   = os.getenv("PRIMARY_MODEL",  "gpt-4o-mini")   # Primary LLM for most agents
WRITER_MODEL    = os.getenv("WRITER_MODEL",   "claude-sonnet-4-5")  # Writer Agent — best prose
SYNTHESIS_MODEL = os.getenv("SYNTHESIS_MODEL","claude-sonnet-4-5")  # Synthesis Sub-Agent
FALLBACK_MODEL  = os.getenv("FALLBACK_MODEL", "gpt-4o-mini")   # Activated if primary fails

# ─── Temperature Ladder ────────────────────────────────────────────────────────
# Principled ladder: 0.1 planning → 0.2 research → 0.3 writing
TEMP_PLANNER     = 0.1   # Deterministic structured planning
TEMP_RESEARCHER  = 0.2   # Slightly varied for different query phrasing on retry
TEMP_SYNTHESIS   = 0.2   # Readable prose, tightly constrained by anti-hallucination prompt
TEMP_VALIDATOR   = 0.1   # Maximally stable binary validation output
TEMP_WRITER      = 0.3   # Most creative — natural non-AI-sounding prose
TEMP_RETRY_AGENT = 0.1   # Binary pass/fail — must be deterministic

# ─── Retry & Timeout ──────────────────────────────────────────────────────────
MAX_RETRIES    = int(os.getenv("MAX_RETRIES",   "5"))    # Global pipeline retry limit
MAX_HV_RETRIES = int(os.getenv("MAX_HV_RETRIES","5"))    # Human validation sub-loop limit
RETRY_DELAY    = int(os.getenv("RETRY_DELAY",   "2"))    # Linear back-off base (seconds)
API_TIMEOUT    = int(os.getenv("API_TIMEOUT",   "120"))  # Seconds before fallback triggers
MAX_GRAPH_LOOPS = 30                                      # LangGraph recursion_limit safety cap

# ─── Context Efficiency Targets (Context Engineering lecture) ─────────────────
TARGET_CFR_MIN = 0.70   # Context Fill Rate minimum (under = wasted capacity)
TARGET_CFR_MAX = 0.85   # Context Fill Rate maximum (over = overflow risk)
TARGET_RTR     = 0.80   # Relevant Token Ratio minimum (below = noise)
TARGET_CUS     = 0.60   # Context Utilization Score minimum (RTR × CFR)

# ─── Eval Harness ─────────────────────────────────────────────────────────────
EVAL_SUCCESS_RATE      = 0.90   # 90% of generations must pass all 5 oracles
EVAL_RUNS_PER_SCENARIO = 5

# ─── File Paths ───────────────────────────────────────────────────────────────
SOUL_MD          = "context/SOUL.md"
USER_MD          = "context/USER.md"
AGENTS_MD        = "context/AGENTS.md"
TOOLS_MD         = "context/TOOLS.md"
RESEARCH_WEB_MD  = "pipeline_context/RESEARCH_WEB.md"
RESEARCH_FIN_MD  = "pipeline_context/RESEARCH_FINANCE.md"
CONTEXT_MD       = "pipeline_context/CONTEXT.md"
VALIDATION_MD    = "pipeline_context/VALIDATION.md"
BRIEF_MD         = "pipeline_context/BRIEF.md"

# ─── Brief Requirements ───────────────────────────────────────────────────────
REQUIRED_SECTIONS = [
    "Company Overview",
    "Products & Services",
    "Financial Snapshot",
    "Top 3 Competitors",
    "Recent News",
    "Strategic Assessment",
]
MIN_SENTENCES_PER_SECTION = 3

# ─── API Keys (read-only; never hardcode) ─────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY",    "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY",    "")
