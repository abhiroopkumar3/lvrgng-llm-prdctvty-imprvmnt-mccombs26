# oracles.py
# Oracle validators O1–O5. All are deterministic Python — zero LLM calls.
# "Oracles check outputs. Evals check the agent." — Evaluation lecture
# These are Tool code enforcing spec invariants, not LLM judgment.

import re
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from state import State

from config import REQUIRED_SECTIONS, MIN_SENTENCES_PER_SECTION

logger = logging.getLogger(__name__)

# ─── Sentence splitter helper ─────────────────────────────────────────────────

def _count_sentences(text: str) -> int:
    """Approximate sentence count using punctuation heuristic."""
    if not text or not text.strip():
        return 0
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return len([s for s in sentences if len(s.strip()) > 10])


def _extract_section(brief: str, section_name: str) -> str:
    """Extract content of a named section from the brief markdown."""
    # Match ## N. Section Name or ## Section Name
    pattern = rf'##\s+(?:\d+\.\s+)?{re.escape(section_name)}\s*\n(.*?)(?=\n##\s|\Z)'
    match = re.search(pattern, brief, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


# ─── O1 — Six-Section Completeness ───────────────────────────────────────────

def oracle_o1_section_completeness(brief: str) -> dict:
    """
    O1 — Six-Section Completeness
    Checks: all six required headers present, non-empty, >= 3 sentences each.
    Returns: {"passed": bool, "violations": list[str]}
    """
    violations = []

    for section in REQUIRED_SECTIONS:
        # Check header present (flexible matching)
        pattern = rf'##\s+(?:\d+\.\s+)?{re.escape(section)}'
        if not re.search(pattern, brief, re.IGNORECASE):
            violations.append(f"Section '{section}' header missing from brief")
            continue

        content = _extract_section(brief, section)
        if not content:
            violations.append(f"Section '{section}' is empty")
            continue

        sentence_count = _count_sentences(content)
        if sentence_count < MIN_SENTENCES_PER_SECTION:
            violations.append(
                f"Section '{section}' has {sentence_count} sentences "
                f"(minimum {MIN_SENTENCES_PER_SECTION} required)"
            )

    return {"passed": len(violations) == 0, "violations": violations}


# ─── O2 — No Data Fabrication ─────────────────────────────────────────────────

def oracle_o2_no_fabrication(brief: str, research: str) -> dict:
    """
    O2 — No Data Fabrication
    Checks Financial Snapshot section: any dollar/percentage/number present
    must be traceable to the research document, OR the section must explicitly
    state data is unavailable.
    Returns: {"passed": bool, "violations": list[str]}
    """
    violations = []

    financial_section = _extract_section(brief, "Financial Snapshot")
    if not financial_section:
        violations.append("Financial Snapshot section not found — O2 cannot verify")
        return {"passed": False, "violations": violations}

    # If section explicitly states unavailability, no fabrication possible
    unavailable_markers = [
        "[data unavailable",
        "data is not available",
        "data unavailable",
        "not publicly available",
        "private company",
        "could not be verified",
    ]
    lower_fin = financial_section.lower()
    if any(m in lower_fin for m in unavailable_markers):
        return {"passed": True, "violations": []}

    # Extract financial figures from the financial section
    # Pattern: dollar amounts and percentages only — NOT bare years (2024 etc.)
    number_pattern = r'\$[\d,\.]+[BMKTbmkt]?|\d+(?:\.\d+)?%'
    numbers_in_brief = re.findall(number_pattern, financial_section)

    for num in numbers_in_brief:
        # Check if this number appears anywhere in research
        # Normalize: remove $ , . for fuzzy matching
        clean = re.sub(r'[$,]', '', num)
        if clean not in research and num not in research:
            violations.append(
                f"Financial figure '{num}' in brief not traceable to research document"
            )

    return {"passed": len(violations) == 0, "violations": violations}


# ─── O3 — Competitor Coverage ─────────────────────────────────────────────────

def oracle_o3_competitor_coverage(brief: str) -> dict:
    """
    O3 — Competitor Coverage
    Checks: Top 3 Competitors section has exactly three named competitors,
    each with (a) competition reason and (b) a differentiator.
    Returns: {"passed": bool, "violations": list[str]}
    """
    violations = []

    section = _extract_section(brief, "Top 3 Competitors")
    if not section:
        violations.append("Top 3 Competitors section not found")
        return {"passed": False, "violations": violations}

    # Count numbered competitors (1. 2. 3. format) or bullet points
    competitor_patterns = [
        r'^\s*\d+\.',          # 1. 2. 3.
        r'^\s*[-*•]',          # - * •
        r'^\*\*[A-Z]',         # **CompanyName
    ]
    lines = section.split('\n')
    competitor_lines = []
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        for pat in competitor_patterns:
            if re.match(pat, line_stripped):
                competitor_lines.append(line_stripped)
                break

    # Also count via "**CompanyName**" bold headers as competitors
    bold_names = re.findall(r'\*\*([^*]+)\*\*', section)
    # Filter to likely company names (capitalized, not generic words)
    excluded = {'company', 'overview', 'competitor', 'differentiator', 'why', 'key'}
    company_names = [n for n in bold_names if n[0].isupper() and n.lower() not in excluded]

    # Use whichever count method finds more competitors
    competitor_count = max(len(competitor_lines), len(company_names))

    if competitor_count < 3:
        violations.append(
            f"Top 3 Competitors section has {competitor_count} competitors identified "
            f"(exactly 3 required)"
        )
    elif competitor_count > 5:
        violations.append(
            f"Top 3 Competitors section has {competitor_count} competitors "
            f"(maximum 3–4 expected)"
        )

    # Check each competitor entry has substance (> 1 sentence worth)
    if competitor_count >= 3:
        sentence_count = _count_sentences(section)
        if sentence_count < 3:
            violations.append(
                "Competitor section lacks sufficient detail (fewer than 3 sentences total)"
            )

    return {"passed": len(violations) == 0, "violations": violations}


# ─── O4 — Recency Check ───────────────────────────────────────────────────────

def oracle_o4_recency_check(brief: str) -> dict:
    """
    O4 — Recency Check
    Checks: Recent News section has >= 2 items; no items clearly older than 12 months.
    Returns: {"passed": bool, "violations": list[str]}
    """
    violations = []

    section = _extract_section(brief, "Recent News")
    if not section:
        violations.append("Recent News section not found")
        return {"passed": False, "violations": violations}

    # Count news items (numbered list, bullets, or bold headers)
    item_count = 0
    lines = section.split('\n')
    for line in lines:
        stripped = line.strip()
        if re.match(r'^\d+\.', stripped) or re.match(r'^[-*•]', stripped):
            item_count += 1

    # If no list items, count by paragraph breaks
    if item_count == 0:
        paragraphs = [p.strip() for p in section.split('\n\n') if p.strip()]
        item_count = len(paragraphs)

    if item_count < 2:
        violations.append(
            f"Recent News section has {item_count} items (minimum 2 required)"
        )

    # Check for dates that are clearly more than 12 months old
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=365)
    cutoff_year = cutoff_date.year

    # Find year mentions
    years_found = re.findall(r'\b(20\d\d)\b', section)
    for year_str in years_found:
        year = int(year_str)
        if year < cutoff_year:
            violations.append(
                f"Recent News section references year {year}, which may be older than 12 months"
            )
            break  # one violation is enough

    return {"passed": len(violations) == 0, "violations": violations}


# ─── O5 — Anti-Hallucination Trace ───────────────────────────────────────────

def oracle_o5_anti_hallucination_trace(brief: str, research: str) -> dict:
    """
    O5 — Anti-Hallucination Trace
    Checks: numerical claims and key proper nouns in the brief
    are traceable to the research document.
    Returns: {"passed": bool, "violations": list[str], "trace_rate": float}
    """
    violations = []

    if not research or not research.strip():
        # No research to compare against — cannot verify
        return {
            "passed": True,
            "violations": ["O5 skipped — research document is empty"],
            "trace_rate": 1.0,
        }

    research_lower = research.lower()
    brief_lower = brief.lower()

    # Extract numerical claims from brief (dollar amounts, percentages, large numbers)
    numbers = re.findall(r'\$[\d,\.]+[BMKTbmkt]?|\d+(?:\.\d+)?%|\b\d{4,}\b', brief)
    total_claims = len(numbers)
    traceable = 0
    untraceable = []

    for num in numbers:
        clean = re.sub(r'[$,]', '', num).lower()
        if clean in research_lower or num.lower() in research_lower:
            traceable += 1
        else:
            # Allow ±10% fuzzy match for rounded numbers
            untraceable.append(num)

    trace_rate = (traceable / total_claims) if total_claims > 0 else 1.0

    # Flag untraceable numbers (allow up to 20% slack for minor rounding)
    if trace_rate < 0.80 and untraceable:
        violations.append(
            f"O5: {len(untraceable)} numerical claims not traceable to research: "
            f"{', '.join(untraceable[:5])}"
        )

    return {
        "passed":       len(violations) == 0,
        "violations":   violations,
        "trace_rate":   round(trace_rate, 3),
        "claims_checked": total_claims,
        "traceable":    traceable,
    }


# ─── Aggregate Oracle Runner ──────────────────────────────────────────────────

def run_all_oracles(state: "State") -> dict:
    """
    Run all 5 oracles against state.brief and state.research.
    Returns aggregate PASS/FAIL with per-oracle results.
    Used by Retry Agent (Agent 7) and eval harness.
    """
    brief    = state.get("brief", "") or state.get("draft", "")
    research = state.get("research", "")

    results = {
        "O1": oracle_o1_section_completeness(brief),
        "O2": oracle_o2_no_fabrication(brief, research),
        "O3": oracle_o3_competitor_coverage(brief),
        "O4": oracle_o4_recency_check(brief),
        "O5": oracle_o5_anti_hallucination_trace(brief, research),
    }

    all_passed = all(r["passed"] for r in results.values())
    results["overall_passed"] = all_passed

    if not all_passed:
        failing = [k for k, v in results.items() if k != "overall_passed" and not v["passed"]]
        results["failing_oracles"] = failing
        results["summary"] = f"FAIL — oracles failed: {', '.join(failing)}"
    else:
        results["failing_oracles"] = []
        results["summary"] = "PASS — all 5 oracles passed"

    return results
