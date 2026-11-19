"""sec_xbrl.py - free fallback for company metrics: SEC EDGAR XBRL "company facts".

Used when FINANCIAL_DATASETS_API_KEY is missing or rejected. Takes the two latest fiscal years
from 10-K filings and computes the same CompanyMetrics fields. EDGAR has no share prices, so
pe_ratio and market_cap stay None (the memo says so instead of guessing).
"""
from __future__ import annotations

from datetime import date

from deepsearch.common import CompanyMetrics
from deepsearch.tools.http import cached, get_json
from deepsearch.tools.sec_filings import _cik, _headers

TTL = 7 * 24 * 3600
REVENUE_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"]


def _annual(facts: dict, tags: list[str], unit: str = "USD") -> dict[int, float]:
    """fiscal-year-end year -> value, from full-year (about 365-day) 10-K facts.

    Companies switch tags over time (Apple used `Revenues` until 2018, then
    `RevenueFromContractWithCustomerExcludingAssessedTax`), so facts from ALL tags are merged
    and, per year, the most recently filed value wins (that also picks up restatements)."""
    out: dict[int, tuple[str, float]] = {}
    for tag in tags:
        node = facts.get("us-gaap", {}).get(tag)
        if not node:
            continue
        for f in node.get("units", {}).get(unit, []):
            if f.get("form") != "10-K" or "start" not in f:
                continue
            days = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days
            if not 350 <= days <= 380:
                continue
            year = int(f["end"][:4])
            if year not in out or f["filed"] > out[year][0]:
                out[year] = (f["filed"], float(f["val"]))
    return {y: v for y, (_, v) in out.items()}


async def get_metrics_from_sec(ticker: str) -> CompanyMetrics:
    ticker = ticker.upper()

    async def fetch():
        cik = await _cik(ticker)
        data = await get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers=_headers())
        f = data["facts"]
        return {"revenue": _annual(f, REVENUE_TAGS), "gross": _annual(f, ["GrossProfit"]),
                "operating": _annual(f, ["OperatingIncomeLoss"]),
                "eps": _annual(f, ["EarningsPerShareDiluted", "EarningsPerShareBasic"], "USD/shares")}

    raw = await cached("sec-xbrl", {"ticker": ticker}, TTL, fetch)
    rev = {int(k): v for k, v in raw["revenue"].items()}
    if not rev:
        raise ValueError(f"no annual revenue in SEC XBRL facts for {ticker}")
    y = max(rev)
    pick = lambda d: {int(k): v for k, v in d.items()}.get(y)  # noqa: E731
    prior = rev.get(y - 1)
    revenue = rev[y]
    return CompanyMetrics(
        ticker=ticker,
        revenue=round(revenue / 1_000_000, 1),
        revenue_growth=round((revenue / prior - 1) * 100, 1) if prior else None,
        eps=pick(raw["eps"]),
        gross_margin=round(pick(raw["gross"]) / revenue * 100, 1) if pick(raw["gross"]) is not None else None,
        operating_margin=round(pick(raw["operating"]) / revenue * 100, 1) if pick(raw["operating"]) is not None else None,
        pe_ratio=None,
        market_cap=None,
        source=f"SEC EDGAR XBRL company facts, fiscal year ending {y} (https://data.sec.gov/api/xbrl/companyfacts/)",
    )
