"""web_search.py - recent news from trusted publishers only (Section 5.7), via Tavily.
AWS edition: 6-hour cache so re-plans and repeated questions don't spend the free quota."""
from __future__ import annotations

import os

from tavily import AsyncTavilyClient

from deepsearch.tools.http import cached

TRUSTED = ["reuters.com", "bloomberg.com", "wsj.com", "cnbc.com", "seekingalpha.com", "ft.com"]
TTL = 6 * 3600


async def search_financial_news(query: str) -> str:
    async def fetch():
        client = AsyncTavilyClient(api_key=os.environ["TAVILY_API_KEY"])
        response = await client.search(query[:380], topic="news", include_domains=TRUSTED, max_results=5)
        return [{"title": h["title"], "url": h["url"], "content": h["content"][:1200]}
                for h in response.get("results", [])]

    hits = await cached("news", {"q": query}, TTL, fetch)
    if not hits:
        return f"No recent news found for: {query}"
    return "\n\n".join(f"{h['title']} ({h['url']})\n{h['content']}" for h in hits)
