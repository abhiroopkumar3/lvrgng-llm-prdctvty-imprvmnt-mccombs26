# Multi-Agent Competitive Intelligence System

**McCombs School of Business, UT Austin | Spring 2026**  

---

## Overview

Given a company name as input, this system produces a structured six-section **Competitive Intelligence Brief** covering:

1. Company Overview
2. Products & Services
3. Financial Snapshot
4. Top 3 Competitors
5. Recent News
6. Strategic Assessment

The pipeline is coordinated by a **deterministic Supervisor** that routes between 10 specialized agents — a Planner, parallel Web Search and Financial Analyst sub-agents, a Synthesis agent, a Writer, Validators, a Retry gate, and an Audit Log agent — all communicating through a single typed `State` object. No agent calls another agent directly. The LLM produces text; Python decides what happens next.

---

## Architecture

```
UI (Browser)
    │
    ▼
FastAPI Server (ui.py)
    │
    ▼
Supervisor Agent  ←───────────────────────────────────────────┐
    │                                                         │
    ├─► Planner Agent (GPT-4o-mini, temp=0.1)                 │
    │                                                         │
    ├─► Researcher Orchestrator                               │
    │       ├─► Web Search Sub-Agent  ─┐  (parallel)          │
    │       ├─► Financial Sub-Agent   ─┘                      │
    │       ├─► Synthesis Sub-Agent (GPT-4o-mini, temp=0.2)   │
    │       └─► Human Validator Gate (auto-accept)            │
    │                                                         │
    ├─► Writer Agent (GPT-4o-mini, temp=0.3)                  │
    │                                                         │
    ├─► Updater / Validators                                  │
    │       ├─► Data Validation Sub-Agent                     │
    │       └─► Structure Validation Sub-Agent                │
    │                                                         │
    ├─► Retry Agent (binary gate → promotes draft to brief) ──┘
    │
    ├─► UI Agent (Outgoing — streams brief to browser)
    │
    └─► Audit Log Agent (writes pipeline_context/CONTEXT.md)
```

**Key design principles:**

- The Supervisor is **pure Python** — it never calls an LLM
- Agents communicate **only through the State object** — no direct calls
- Every external API call is wrapped in `call_with_retry()` with linear back-off
- Missing data is never fabricated — marked as `[Data unavailable — not found in source research]`
- Web search: Tavily (primary) → DuckDuckGo (automatic fallback, no key needed)
- Financial data: yfinance (primary) → web search fallback for private companies

---

## Project Structure

```
multi_agent_ci/
├── main.py                      # Entry point — starts server + opens browser
├── agents.py                    # All 10 agents + LangGraph StateGraph
├── state.py                     # TypedDict State definition
├── tools.py                     # Tool wrappers (Tavily, DuckDuckGo, yfinance, Wikipedia)
├── config.py                    # All constants (models, temperatures, retry params, paths)
├── oracles.py                   # Oracle validators O1–O5 (deterministic Python)
├── ui.py                        # FastAPI server with polling and PDF download
├── index.html                   # Single-page chat UI with session history
├── requirements.txt             # All Python dependencies
├── .env.example                 # API key template — copy to .env
├── context/
│   ├── SOUL.md                  # Agent personality and values
│   ├── USER.md                  # User preferences
│   ├── AGENTS.md                # Agent behavioral rules
│   └── TOOLS.md                 # Tool usage guidance
├── pipeline_context/            # Runtime-written audit mirrors (not read during pipeline)
│   ├── RESEARCH_WEB.md
│   ├── RESEARCH_FINANCE.md
│   ├── CONTEXT.md               # Full audit log (written after completion)
│   ├── VALIDATION.md
│   └── BRIEF.md
└── eval/
    ├── ci_eval_config.yaml      # Eval scenarios and pass thresholds
    └── run_eval.py              # Eval harness runner
```

---

## Setup Instructions

### Prerequisites

- **Python 3.12** (required)
- **Anaconda or Miniconda** (recommended for environment management)

### Step 1 — Create a Python 3.12 environment

```bash
conda create -n multi_agent_ci python=3.12.9 -y
conda activate multi_agent_ci
```

> **Windows users:** After activating, your terminal prompt will show `(multi_agent_ci)` instead of `(base)`.

### Step 2 — Install dependencies

```bash
cd multi_agent_ci
pip install -r requirements.txt
```

This installs all required packages including LangGraph, FastAPI, OpenAI SDK, search tools, yfinance, and reportlab for PDF generation. Takes approximately 2–4 minutes.

---

## API Key Setup

Copy the template and fill in your keys:

```bash
# macOS / Linux
cp .env.example .env

# Windows (Command Prompt)
copy .env.example .env
```

Open `.env` in any text editor and fill in:

```env
OPENAI_API_KEY=your-openai-key-here
ANTHROPIC_API_KEY=your-anthropic-key-here                              # Optional — leave blank if using OpenAI only
TAVILY_API_KEY=tvly-your-tavily-key-here        # Recommended — free at tavily.com
```

### Where to get each key

| Key | Where to get it | Required? | Cost |
|-----|----------------|-----------|------|
| `OPENAI_API_KEY` | platform.openai.com → API Keys | **Yes** | ~$0.04/run |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys | No | ~$0.02/run (better prose) |
| `TAVILY_API_KEY` | tavily.com → Dashboard | Recommended | Free (1,000/month) |

> **OpenAI only:** If you do not have an Anthropic key, open `config.py` and change:
>
> ```python
> WRITER_MODEL    = "gpt-4o-mini"
> SYNTHESIS_MODEL = "gpt-4o-mini"
> ```
>
> The system will then use GPT-4o-mini for all agents.

> **No Tavily key:** The system automatically falls back to DuckDuckGo (no key needed). Research quality is slightly lower but the pipeline runs identically.

---

## How to Run

```bash
conda activate multi_agent_ci
cd multi_agent_ci
python main.py
```

Your browser will open automatically to `http://localhost:8000`.

If the browser does not open automatically, navigate there manually.

To stop the server: press `Ctrl+C` in the terminal.

---

## Using the System

1. Type a company name in the input field at the bottom (e.g., `Nvidia`, `Apple`, `Chipotle`)
2. Press **Enter** or click **Generate Brief**
3. Watch the progress bar and pipeline log update in real time as each agent fires
4. The brief appears in the chat window when complete (~2–3 minutes for a well-known company)
5. Click **Download PDF** inside the brief card to save as PDF

**Session history:** The UI retains the last 3 company briefs simultaneously. Each brief has its own Download PDF button so it is always clear which brief is being downloaded.

---

## Failure Handling

The system handles failures gracefully at every layer:

| Failure | What happens |
|---------|-------------|
| Tavily API unavailable | Automatically falls back to DuckDuckGo |
| Company not publicly traded | yfinance returns unavailable; web search used for funding data |
| Unknown/obscure company | Wikipedia fallback; sections marked `[Data unavailable]` |
| LLM timeout | Retry with linear back-off (up to 5 attempts) |
| Max retries exceeded | Partial brief delivered with PARTIAL OUTPUT NOTICE footer |

---

## Running the Eval Harness (Optional)

```bash
# Dry run — tests harness logic only, no API calls
python eval/run_eval.py --dry-run

# Single scenario, 1 run (cheapest to test)
python eval/run_eval.py --scenario tesla_public_large --runs 1

# Full eval — 5 scenarios x 5 runs = 25 total (~$1.00)
python eval/run_eval.py
```

Results saved to `eval/eval_report_{timestamp}.md`.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `langgraph` | Supervisor-Worker graph with typed State and conditional routing |
| `langchain` + `langchain-openai` + `langchain-anthropic` | LLM provider abstraction |
| `openai` | GPT-4o-mini for Planner, Validators, Retry Agent, Audit Log |
| `anthropic` | Claude Sonnet for Writer + Synthesis (optional, better prose) |
| `tavily-python` | Primary web search — agent-optimized, structured results |
| `duckduckgo-search` | Fallback web search — no API key required |
| `yfinance` | Yahoo Finance data for public company financials |
| `wikipedia` | Last-resort fallback for basic company info |
| `fastapi` + `uvicorn` | Web server with polling-based real-time updates |
| `reportlab` | PDF generation from brief markdown |
| `python-dotenv` | API key management from `.env` file |
| `pyyaml` | Eval config parsing |

---

## Troubleshooting

**`ModuleNotFoundError` on startup:**
You are not in the correct conda environment. Run `conda activate multi_agent_ci` first.

**Port 8000 already in use (Windows):**

```cmd
netstat -ano | findstr :8000
taskkill /PID <pid> /F
```

**Brief shows `[Data unavailable]` in Financial Snapshot:**
Expected behavior for private companies (Stripe, Databricks, etc.) or when yfinance cannot find a ticker. Not a bug — the system never fabricates financial figures.

**PDF downloads as `.txt`:**
Run `pip install reportlab --force-reinstall` then restart with `python main.py`.

**Pipeline takes longer than 3 minutes:**
Normal for obscure companies where multiple search retries are needed. The terminal log will show each agent firing. The brief will appear when complete.

---

## Every-Time Startup

```bash
conda activate multi_agent_ci
cd "path/to/multi_agent_ci"
python main.py
```
