"""financial_data.py - fetch ONE company's numbers and clean them up (Section 5.7).

Financial Datasets API: last two annual income statements + a price snapshot -> CompanyMetrics.
AWS edition: shared HTTP helper (retries, 429 backoff) and a 7-day cache.
"""
from __future__ import annotations

import os

import httpx

from deepsearch.common import CompanyMetrics
from deepsearch.tools.http import cached, get_json

BASE_URL = "https://api.financialdatasets.ai"
TTL = 7 * 24 * 3600


def _pct(part, whole):
    """part / whole as a percent (1 decimal), or None if we can't work it out."""
    if part is None or not whole:
        return None
    return round(part / whole * 100, 1)


async def _fetch_raw(ticker: str) -> dict:
    headers = {"X-API-KEY": os.environ["FINANCIAL_DATASETS_API_KEY"]}
    income = await get_json(f"{BASE_URL}/financials/income-statements", headers=headers,
                            params={"ticker": ticker, "period": "annual", "limit": 2})
    price = await get_json(f"{BASE_URL}/prices/snapshot", headers=headers, params={"ticker": ticker})
    return {"income_statements": income.get("income_statements", []), "snapshot": price.get("snapshot", {})}


async def get_financial_metrics(ticker: str) -> CompanyMetrics:
    """Financial Datasets API when a working key is set; otherwise SEC EDGAR XBRL (free)."""
    ticker = ticker.upper()
    if os.getenv("FINANCIAL_DATASETS_API_KEY"):
        try:
            return await _from_financial_datasets(ticker)
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (401, 402, 403):   # bad key / plan limit → fall back
                raise
    from deepsearch.tools.sec_xbrl import get_metrics_from_sec

    return await get_metrics_from_sec(ticker)


async def _from_financial_datasets(ticker: str) -> CompanyMetrics:
    raw = await cached("fin", {"ticker": ticker}, TTL, lambda: _fetch_raw(ticker))
    statements = raw["income_statements"]
    if not statements:
        raise ValueError(f"no income statements for {ticker}")
    latest = statements[0]
    prior = statements[1] if len(statements) > 1 else None
    snapshot = raw["snapshot"] or {}
    price = snapshot.get("price") or snapshot.get("close")

    revenue = latest.get("revenue")
    eps = latest.get("earnings_per_share")
    growth = None
    if revenue and prior and prior.get("revenue"):
        growth = round((revenue / prior["revenue"] - 1) * 100, 1)

    return CompanyMetrics(
        ticker=ticker,
        revenue=round(revenue / 1_000_000, 1) if revenue else None,   # $ millions
        revenue_growth=growth,
        eps=eps,
        gross_margin=_pct(latest.get("gross_profit"), revenue),
        operating_margin=_pct(latest.get("operating_income"), revenue),
        pe_ratio=round(price / eps, 1) if price and eps and eps > 0 else None,
        market_cap=snapshot.get("market_cap"),
        source="Financial Datasets API (https://financialdatasets.ai)",
    )
