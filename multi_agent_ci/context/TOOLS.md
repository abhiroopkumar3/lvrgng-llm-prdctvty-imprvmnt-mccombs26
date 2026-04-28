# TOOLS.md — Tool Usage Guidance

## Search Tools
- Tavily: primary; use for company overview, news, competitors
- DuckDuckGo: fallback if Tavily fails or quota exhausted
- Wikipedia: fallback path only (Fallback Agent mode d)

## Financial Tools
- yfinance: primary for public companies; ticker lookup first
- Financial news search via Tavily/DuckDuckGo: secondary for funding/private company data

## Usage Rules
- All tool calls wrapped in call_with_retry()
- All tools return structured dicts with "status" key
- Agents check result["status"] == "ok" before using data
- Never parse raw HTML; use tool-level content extraction

## Rate Limits
- Tavily: 1,000 searches/month (free tier)
- DuckDuckGo: no rate limit
- yfinance: no rate limit for public data
- Wikipedia: no rate limit

## Fallback Chain
Tavily → DuckDuckGo → Wikipedia
(Wikipedia only on Fallback Agent mode d)
