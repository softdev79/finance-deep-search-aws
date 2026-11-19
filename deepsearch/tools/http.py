"""http.py - shared HTTP helper for the tools: timeouts, retries with backoff on 429/5xx,
and a cache (DynamoDB on AWS, a local JSON folder otherwise) so the same filing or the same
quarter's numbers are fetched once (Section 12: "cache filed numbers, they never change")."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import time
from pathlib import Path

import httpx

CACHE_TABLE = os.getenv("CACHE_TABLE", "")
LOCAL_CACHE = Path(__file__).resolve().parents[2] / ".cache"


async def get_json(url: str, *, headers: dict | None = None, params: dict | None = None,
                   attempts: int = 4) -> dict:
    return json.loads(await get_text(url, headers=headers, params=params, attempts=attempts))


async def get_text(url: str, *, headers: dict | None = None, params: dict | None = None,
                   attempts: int = 4) -> str:
    delay = 1.0
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        for i in range(attempts):
            r = await c.get(url, headers=headers, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                if i == attempts - 1:
                    r.raise_for_status()
                retry_after = r.headers.get("retry-after")
                await asyncio.sleep(float(retry_after) if retry_after and retry_after.isdigit()
                                    else delay + random.random())
                delay *= 2
                continue
            r.raise_for_status()  # 401 bad key, 404 unknown ticker: fail fast
            return r.text
    raise RuntimeError("unreachable")


def _key(namespace: str, args: dict) -> str:
    return namespace + "#" + hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:32]


async def cached(namespace: str, args: dict, ttl_seconds: int, fetch):
    """Return the cached value for (namespace, args) or call `fetch()` and store it."""
    key = _key(namespace, args)
    hit = await asyncio.to_thread(_cache_get, key)
    if hit is not None:
        return hit
    value = await fetch()
    await asyncio.to_thread(_cache_put, key, value, ttl_seconds)
    return value


def _cache_get(key: str):
    now = int(time.time())
    if CACHE_TABLE:
        import boto3

        item = boto3.resource("dynamodb").Table(CACHE_TABLE).get_item(Key={"pk": key}).get("Item")
        if item and int(item["expires_at"]) > now:
            return json.loads(item["value"])
        return None
    f = LOCAL_CACHE / f"{key.replace('#', '_')}.json"
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        if data["expires_at"] > now:
            return data["value"]
    return None


def _cache_put(key: str, value, ttl: int) -> None:
    expires = int(time.time()) + ttl
    if CACHE_TABLE:
        import boto3

        body = json.dumps(value)
        if len(body) < 350_000:  # DynamoDB item limit is 400 KB
            boto3.resource("dynamodb").Table(CACHE_TABLE).put_item(
                Item={"pk": key, "value": body, "expires_at": expires})
        return
    LOCAL_CACHE.mkdir(exist_ok=True)
    (LOCAL_CACHE / f"{key.replace('#', '_')}.json").write_text(
        json.dumps({"expires_at": expires, "value": value}), encoding="utf-8")
