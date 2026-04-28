# tools.py
# All external tool wrappers. Every external call goes through call_with_retry().
# All tools return structured dicts with a "status" key — never raise to agents.

import time
import hashlib
import logging
from typing import Callable, Any
from config import MAX_RETRIES, RETRY_DELAY, TAVILY_API_KEY

logger = logging.getLogger(__name__)


# ─── Universal Retry Wrapper ──────────────────────────────────────────────────

def call_with_retry(
    fn: Callable,
    label: str,
    max_retries: int = MAX_RETRIES,
    delay: int = RETRY_DELAY,
) -> Any:
    """
    Wraps any callable with linear back-off retry.
    Matches the LangGraph lecture call_with_retry() pattern exactly.

    On each failure: logs attempt, sleeps RETRY_DELAY × attempt seconds.
    After max_retries exhausted: raises RuntimeError with structured message.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as e:
            msg = f"[{label}] Failed (attempt {attempt}/{max_retries}): {e}"
            logger.warning(msg)
            print(msg)
            if attempt < max_retries:
                sleep_sec = delay * attempt
                logger.info(f"[{label}] Retrying in {sleep_sec}s...")
                time.sleep(sleep_sec)
    raise RuntimeError(f"[{label}] Failed after {max_retries} retries.")


# ─── Helper ──────────────────────────────────────────────────────────────────

def _hash_params(params: dict) -> str:
    """Hash query params for audit log — never log raw params."""
    return hashlib.md5(str(sorted(params.items())).encode()).hexdigest()[:8]


# ─── Tavily Search Tool ───────────────────────────────────────────────────────

def tavily_search(query: str, max_results: int = 5) -> dict:
    """
    Primary search tool for Web Search Sub-Agent (4a).
    Requires TAVILY_API_KEY environment variable.
    Returns: {"status": "ok", "results": [{title, url, content}, ...]}
             {"status": "unavailable", "reason": str}
    """
    if not TAVILY_API_KEY or TAVILY_API_KEY in ("", "your_tavily_key_here"):
        return {"status": "unavailable", "reason": "TAVILY_API_KEY not configured"}

    def _call():
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_API_KEY)
        response = client.search(query=query, max_results=max_results)
        results = []
        for r in response.get("results", []):
            results.append({
                "title":   r.get("title", ""),
                "url":     r.get("url", ""),
                "content": r.get("content", ""),
            })
        return {"status": "ok", "results": results}

    try:
        return call_with_retry(_call, label=f"tavily:{_hash_params({'q': query})}", max_retries=3)
    except Exception as e:
        logger.warning(f"Tavily failed entirely: {e}")
        return {"status": "unavailable", "reason": str(e)}


# ─── DuckDuckGo Search Tool ───────────────────────────────────────────────────

def duckduckgo_search(query: str, max_results: int = 5) -> dict:
    """
    Fallback search tool. No API key required.
    Returns same schema as tavily_search for drop-in substitution.
    """
    def _call():
        from duckduckgo_search import DDGS
        ddgs = DDGS()
        raw = list(ddgs.text(query, max_results=max_results))
        results = []
        for r in raw:
            results.append({
                "title":   r.get("title", ""),
                "url":     r.get("href", ""),
                "content": r.get("body", ""),
            })
        return {"status": "ok", "results": results}

    try:
        return call_with_retry(_call, label=f"ddg:{_hash_params({'q': query})}", max_retries=3)
    except Exception as e:
        logger.warning(f"DuckDuckGo failed entirely: {e}")
        return {"status": "unavailable", "reason": str(e)}


def web_search(query: str, max_results: int = 5) -> dict:
    """
    Smart search: tries Tavily first, falls back to DuckDuckGo.
    This is the function agents call — they never call tavily_search directly.
    """
    result = tavily_search(query, max_results)
    if result["status"] == "ok" and result.get("results"):
        result["source"] = "tavily"
        return result

    logger.info(f"Tavily unavailable for '{query}', falling back to DuckDuckGo")
    print(f"[TOOLS] Tavily failed — fallback to DuckDuckGo for: {query}")
    result = duckduckgo_search(query, max_results)
    if result["status"] == "ok":
        result["source"] = "duckduckgo"
    return result


# ─── yfinance Financial Tool ──────────────────────────────────────────────────

def yfinance_lookup(company_name: str) -> dict:
    """
    Primary financial data tool for Financial Analyst Sub-Agent (4b).
    Attempts ticker lookup from company name, then fetches financials.
    Returns: {"status": "ok", "ticker": str, "revenue": ..., "net_income": ...,
              "market_cap": ..., "growth_rate": ..., "employees": ..., "sector": ...,
              "competitors": [...], "data_date": str}
             {"status": "unavailable", "reason": str}
    Never raises — always returns structured dict.
    """
    def _call():
        import yfinance as yf

        # Attempt to resolve ticker
        ticker_symbol = _resolve_ticker(company_name)
        if not ticker_symbol:
            return {"status": "unavailable", "reason": f"No ticker found for '{company_name}'"}

        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info or {}

        if not info or info.get("regularMarketPrice") is None and info.get("marketCap") is None:
            return {"status": "unavailable", "reason": f"No data returned for ticker {ticker_symbol}"}

        # Revenue from financials
        revenue = None
        growth_rate = None
        try:
            financials = ticker.financials
            if financials is not None and not financials.empty:
                rev_row = financials[financials.index == "Total Revenue"]
                if not rev_row.empty:
                    vals = rev_row.values[0]
                    if len(vals) >= 1:
                        revenue = int(vals[0]) if vals[0] else None
                    if len(vals) >= 2 and vals[0] and vals[1]:
                        growth_rate = round((vals[0] - vals[1]) / abs(vals[1]) * 100, 1)
        except Exception:
            pass

        net_income = info.get("netIncomeToCommon")
        market_cap = info.get("marketCap")
        employees  = info.get("fullTimeEmployees")
        sector     = info.get("sector", "[Data unavailable — not found in source research]")

        def _fmt_usd(v):
            if v is None:
                return "[Data unavailable — not found in source research]"
            if abs(v) >= 1e9:
                return f"${v/1e9:.2f}B"
            if abs(v) >= 1e6:
                return f"${v/1e6:.2f}M"
            return f"${v:,.0f}"

        return {
            "status":      "ok",
            "ticker":      ticker_symbol,
            "revenue":     _fmt_usd(revenue),
            "net_income":  _fmt_usd(net_income),
            "market_cap":  _fmt_usd(market_cap),
            "growth_rate": f"{growth_rate}% YoY" if growth_rate is not None else "[Data unavailable — not found in source research]",
            "employees":   str(employees) if employees else "[Data unavailable — not found in source research]",
            "sector":      sector,
            "profitable":  "Yes" if (net_income and net_income > 0) else ("No" if net_income is not None else "[Data unavailable — not found in source research]"),
            "data_date":   time.strftime("%Y-%m-%d"),
        }

    try:
        return call_with_retry(_call, label=f"yfinance:{company_name}", max_retries=3)
    except Exception as e:
        logger.warning(f"yfinance failed entirely for {company_name}: {e}")
        return {"status": "unavailable", "reason": str(e)}


def _resolve_ticker(company_name: str) -> str | None:
    """
    Attempts to find a stock ticker from a company name.
    Tries common mappings first, then yfinance search.
    """
    # Common known mappings for fast resolution
    known = {
        "nvidia": "NVDA", "apple": "AAPL", "microsoft": "MSFT",
        "google": "GOOGL", "alphabet": "GOOGL", "amazon": "AMZN",
        "meta": "META", "tesla": "TSLA", "netflix": "NFLX",
        "salesforce": "CRM", "snowflake": "SNOW", "databricks": "DBX",
        "stripe": None, "openai": None, "rivian": "RIVN",
        "palantir": "PLTR", "shopify": "SHOP", "uber": "UBER",
        "airbnb": "ABNB", "doordash": "DASH", "coinbase": "COIN",
        "intel": "INTC", "amd": "AMD", "qualcomm": "QCOM",
        "oracle": "ORCL", "ibm": "IBM", "cisco": "CSCO",
    }
    lower = company_name.lower().strip()
    if lower in known:
        return known[lower]

    # Try direct ticker lookup
    try:
        import yfinance as yf
        t = yf.Ticker(company_name.upper())
        info = t.info
        if info and info.get("regularMarketPrice"):
            return company_name.upper()
    except Exception:
        pass

    # Try yfinance search
    try:
        import yfinance as yf
        results = yf.Search(company_name, max_results=3)
        quotes = results.quotes
        if quotes:
            return quotes[0].get("symbol")
    except Exception:
        pass

    return None


# ─── Wikipedia Tool ───────────────────────────────────────────────────────────

def wikipedia_lookup(company_name: str) -> dict:
    """
    Fallback tool — used on Fallback Agent path (d) when primary tools fail.
    Returns: {"status": "ok", "summary": str, "url": str}
             {"status": "unavailable", "reason": str}
    """
    def _call():
        import wikipedia
        try:
            page = wikipedia.page(company_name, auto_suggest=True)
            return {
                "status":  "ok",
                "summary": wikipedia.summary(company_name, sentences=10),
                "url":     page.url,
                "title":   page.title,
            }
        except wikipedia.DisambiguationError as e:
            # Retry with first disambiguation option
            if e.options:
                page = wikipedia.page(e.options[0])
                return {
                    "status":  "ok",
                    "summary": page.summary[:2000],
                    "url":     page.url,
                    "title":   page.title,
                }
            raise
        except wikipedia.PageError:
            return {"status": "unavailable", "reason": f"Wikipedia page not found for '{company_name}'"}

    try:
        return call_with_retry(_call, label=f"wikipedia:{company_name}", max_retries=2)
    except Exception as e:
        return {"status": "unavailable", "reason": str(e)}
