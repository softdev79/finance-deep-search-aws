"""sec_filings.py - "Item 1A. Risk Factors" from a company's latest 10-K on SEC EDGAR.

The course uses this tool (Section 5.7) but doesn't show its code; this is a fresh implementation:
1. ticker -> CIK            https://www.sec.gov/files/company_tickers.json
2. latest 10-K              https://data.sec.gov/submissions/CIK##########.json
3. the filing document      https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc>
4. HTML -> text, cut out Item 1A (up to Item 1B / Item 2)
5. Section 12's "intern": the small model squeezes ~20k+ tokens of risk factors into ~10 bullets,
   so the planner and synthesizer never see the raw filing.

SEC requires a User-Agent with contact details (SEC_USER_AGENT) and allows ~10 requests/second.
Results are cached for 30 days (a filed 10-K never changes).
"""
from __future__ import annotations

import os
import re

from bs4 import BeautifulSoup

from deepsearch import llm
from deepsearch.tools.http import cached, get_json, get_text

TTL = 30 * 24 * 3600
MAX_CHARS_TO_SUMMARISE = 60_000  # ~15k tokens into the small model


def _headers() -> dict:
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError("SEC_USER_AGENT is not set (SEC requires a contact in the User-Agent)")
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


async def company_name(ticker: str) -> str | None:
    """SEC-registered name for a ticker (e.g. 'Apple Inc.'), from the same cached ticker list."""
    async def fetch():
        data = await get_json("https://www.sec.gov/files/company_tickers.json", headers=_headers())
        return {row["ticker"].upper(): row["title"] for row in data.values()}

    return (await cached("sec-names", {}, TTL, fetch)).get(ticker.upper())


async def _cik(ticker: str) -> int:
    async def fetch():
        data = await get_json("https://www.sec.gov/files/company_tickers.json", headers=_headers())
        return {row["ticker"].upper(): row["cik_str"] for row in data.values()}

    table = await cached("sec-tickers", {}, TTL, fetch)
    if ticker not in table:
        raise ValueError(f"{ticker} not found on SEC EDGAR")
    return int(table[ticker])


def _risk_section(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text)
    # The table of contents mentions Item 1A too; take the LONGEST span between "Item 1A" and the
    # next "Item 1B"/"Item 1C"/"Item 2", which is the real section.
    starts = [m.span() for m in re.finditer(r"item\s*1a\.?\s*[-:—–]?\s*risk\s*factors", text, re.I)]
    best = ""
    for s, head_end in starts:
        end = re.search(r"item\s*1b\.?|item\s*1c\.?|item\s*2\.?\s*propert", text[head_end:], re.I)
        span = text[s: head_end + end.start()] if end else text[s: s + 200_000]
        if len(span) > len(best):
            best = span
    if len(best) < 2_000:
        best = _risk_section_by_heading(text)
    if len(best) < 2_000:
        raise ValueError("could not locate the Risk Factors section in the 10-K")
    return best


def _risk_section_by_heading(text: str) -> str:
    """Fallback for 10-Ks without an 'Item 1A' heading in the body (e.g. Intel's): a 'Risk Factors'
    heading followed by an intro sentence, up to the next major section heading."""
    start = re.search(r"risk\s*factors\s+(the following|this section|we are subject|investing in our)", text, re.I)
    if not start:
        return ""
    end = re.search(r"other key information|unresolved staff comments|item\s*1b\.?|item\s*2\.?\s*propert"
                    r"|quantitative and qualitative disclosures", text[start.end() + 5_000:], re.I)
    stop = start.end() + 5_000 + end.start() if end else start.start() + 200_000
    return text[start.start(): stop]


async def _latest_10k_risks(ticker: str) -> dict:
    cik = await _cik(ticker)
    subs = await get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers=_headers())
    recent = subs["filings"]["recent"]
    for i, form in enumerate(recent["form"]):
        if form == "10-K":
            acc = recent["accessionNumber"][i].replace("-", "")
            doc = recent["primaryDocument"][i]
            filed = recent["filingDate"][i]
            break
    else:
        raise ValueError(f"no 10-K found for {ticker}")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
    section = _risk_section(await get_text(url, headers=_headers()))
    summary = await llm.complete(
        [{"role": "system", "content": "You summarise 10-K risk factors for an equity analyst. "
          "Return 8-12 short bullets, most material first. Keep company-specific facts and numbers; "
          "drop boilerplate. No preamble."},
         {"role": "user", "content": section[:MAX_CHARS_TO_SUMMARISE]}],
        model=llm.MODEL_SMALL,
        reasoning_effort="low",   # summarising, not reasoning: keeps hidden reasoning tokens down
    )
    return {"ticker": ticker, "filed": filed, "url": url, "chars": len(section), "summary": summary.strip()}


async def get_risk_factors(ticker: str) -> str:
    ticker = ticker.upper()
    r = await cached("sec-1a", {"ticker": ticker}, TTL, lambda: _latest_10k_risks(ticker))
    return (f"{r['ticker']} risk factors (10-K filed {r['filed']}, {r['url']}; "
            f"summarised from {r['chars']:,} characters):\n{r['summary']}")
