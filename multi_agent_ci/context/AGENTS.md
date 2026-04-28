# AGENTS.md — Agent Behavioral Rules

## Universal Rules (all agents)
1. Only use information present in your provided context (State fields)
2. Never call another agent directly — communicate only through State fields
3. Always emit an audit entry at start and end of your function
4. On API error: set state.error; do not raise; return partial state
5. On LLM timeout (> 120s): set state.error = "{agent_name} timeout"; return partial state

## Anti-Hallucination Policy
- Missing data marker: "[Data unavailable — not found in source research]"
- Never substitute training data for missing source data
- Every number in the brief must appear in state.research
- Cross-reference check is performed deterministically after every LLM synthesis call

## Format Rules
- No em-dashes (—) — use commas or periods
- No AI-indicative phrases ("it is worth noting", "notably", "importantly")
- Section headers must exactly match REQUIRED_SECTIONS list from config.py
- Each section minimum 3 sentences

## Supervisor Rules
- Supervisor NEVER calls an LLM
- Routing decisions are pure Python conditionals
- Empty string == stage not done yet
- state.retries >= MAX_RETRIES always routes to fallback

## Temperature Policy
- Planner: 0.1 (deterministic)
- Researcher: 0.2 (slightly varied for retry diversity)
- Synthesis: 0.2 (constrained by anti-hallucination prompt)
- Validator: 0.1 (binary output)
- Writer: 0.3 (natural prose)
- Retry Agent: 0.1 (binary gate)
