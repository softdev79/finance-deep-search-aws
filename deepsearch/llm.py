"""llm.py - one place for every LLM call, pointed at Amazon Bedrock.

- Bedrock's OpenAI-compatible endpoint, so the course's `openai` code keeps working.
- No long-lived key: locally the token is signed from your `aws login`; on Lambda from the
  function's IAM role. Refreshed every 30 minutes.
- Two model sizes (Section 12, "small model for small jobs"): LARGE plans, reasons and writes;
  SMALL summarises 10-K text.
- `parse()` returns a Pydantic object. It tries strict structured output first and falls back to
  JSON mode + validation + one repair turn, because not every model on the endpoint supports
  strict JSON schemas.
- Every call adds its token usage to a per-run counter (Section 12 RunBudget).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-2"
BASE_URL = os.getenv("OPENAI_BASE_URL", f"https://bedrock-mantle.{REGION}.api.aws/v1")
PROJECT = os.getenv("OPENAI_PROJECT_ID") or None
MODEL_LARGE = os.getenv("MODEL_LARGE", "openai.gpt-oss-120b")
MODEL_SMALL = os.getenv("MODEL_SMALL", "openai.gpt-oss-20b")

# USD per 1M tokens (input, output), used only for the cost estimate in run records.
PRICES = {"openai.gpt-oss-120b": (0.15, 0.60), "openai.gpt-oss-20b": (0.07, 0.30)}

T = TypeVar("T", bound=BaseModel)
_lock = threading.Lock()
_token: str | None = None
_token_at = 0.0


def _api_key(force_refresh: bool = False) -> str:
    if os.getenv("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    global _token, _token_at
    with _lock:
        # The token can't outlive the AWS credentials that signed it (an `aws login` session
        # rotates within minutes), so refresh often and on any 401.
        if force_refresh or _token is None or time.time() - _token_at > 10 * 60:
            from aws_bedrock_token_generator import provide_token

            _token = provide_token(region=REGION, expiry=timedelta(hours=1))
            _token_at = time.time()
        return _token


def client(force_refresh: bool = False) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=_api_key(force_refresh), base_url=BASE_URL, max_retries=3, timeout=120,
                       default_headers={"OpenAI-Project": PROJECT} if PROJECT else None)


async def _with_fresh_token(fn):
    """Run fn(client); on 401 (expired token) re-sign the token and try once more."""
    from openai import AuthenticationError

    try:
        async with client() as c:
            return await fn(c)
    except AuthenticationError:
        async with client(force_refresh=True) as c:
            return await fn(c)


@dataclass
class Usage:
    """Token and cost counter for one research run (Section 12 RunBudget)."""
    max_tokens: int = int(os.getenv("RUN_MAX_TOKENS", "400000"))
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict = field(default_factory=dict)

    def add(self, model: str, usage) -> None:
        if usage is None:
            return
        i, o = usage.prompt_tokens or 0, usage.completion_tokens or 0
        self.calls += 1
        self.input_tokens += i
        self.output_tokens += o
        pin, pout = PRICES.get(model, (0.15, 0.60))
        self.cost_usd += i / 1e6 * pin + o / 1e6 * pout
        self.by_model[model] = self.by_model.get(model, 0) + i + o
        if self.input_tokens + self.output_tokens > self.max_tokens:
            raise RuntimeError(f"run token budget exceeded ({self.max_tokens:,} tokens)")


USAGE = Usage()  # counter for the CURRENT run; steps.handler resets it per invocation


def start_run_usage(already_used_tokens: int = 0) -> Usage:
    """New counter for one run (or one Step Functions state of a run). The budget left is the run
    budget minus what earlier states already spent, so a warm Lambda serving many runs, or many
    runs in one process, never share a budget."""
    global USAGE
    budget = int(os.getenv("RUN_MAX_TOKENS", "400000"))
    USAGE = Usage(max_tokens=max(budget - already_used_tokens, 1))
    return USAGE


async def complete(messages: list[dict], model: str = MODEL_LARGE, **kw) -> str:
    # clients are closed on exit: Lambda starts a new event loop per invocation
    resp = await _with_fresh_token(lambda c: c.chat.completions.create(model=model, messages=messages, **kw))
    USAGE.add(model, resp.usage)
    return resp.choices[0].message.content or ""


async def parse(messages: list[dict], schema: type[T], model: str = MODEL_LARGE) -> T:
    """Structured output as a Pydantic object, with a JSON-mode fallback and one repair turn."""
    return await _with_fresh_token(lambda c: _parse(c, messages, schema, model))


async def _parse(c: AsyncOpenAI, messages: list[dict], schema: type[T], model: str) -> T:
    # Strict JSON-schema mode, twice. gpt-oss on Bedrock returns the right JSON but sometimes with a
    # stray prefix such as "**", "##" or a broken first key, so we read the raw text and validate it
    # ourselves.
    for _ in range(2):
        try:
            resp = await c.chat.completions.create(
                model=model, messages=messages,
                response_format={"type": "json_schema", "json_schema": {
                    "name": schema.__name__, "schema": _strict_schema(schema), "strict": True}})
            USAGE.add(model, resp.usage)
            return _validate(schema, resp.choices[0].message.content or "")
        except RuntimeError:  # token budget exceeded: stop, don't retry
            raise
        except Exception:     # unsupported mode or still invalid → try again, then JSON mode
            continue

    schema_json = json.dumps(schema.model_json_schema())
    msgs = list(messages) + [{"role": "user", "content":
                              f"Reply with ONLY a JSON object that matches this JSON schema:\n{schema_json}"}]
    last_error = ""
    for _ in range(2):
        resp = await c.chat.completions.create(model=model, messages=msgs,
                                               response_format={"type": "json_object"})
        USAGE.add(model, resp.usage)
        text = resp.choices[0].message.content or ""
        try:
            return _validate(schema, text)
        except (ValidationError, ValueError) as e:
            last_error = str(e)[:1500]
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user", "content": f"That JSON did not validate:\n{last_error}\nReturn corrected JSON only."}]
    raise ValueError(f"model did not return a valid {schema.__name__}: {last_error}")


def _validate(schema: type[T], text: str) -> T:
    """Cut the outermost {...} out of the reply (drops prefixes, fences, chatter) and validate.
    strict=False lets raw newlines inside strings through, which gpt-oss sometimes emits."""
    end = text.rfind("}")
    starts = [i for i, ch in enumerate(text) if ch == "{" and i < end][:25]
    if not starts:
        raise ValueError("no JSON object in the reply")
    last: Exception | None = None
    for start in starts:  # a junk fragment like '{"{' can precede the real object
        try:
            return schema.model_validate(json.loads(text[start:end + 1], strict=False))
        except (json.JSONDecodeError, ValidationError) as e:
            last = e
    raise ValueError(str(last))


def _strict_schema(schema: type[BaseModel]) -> dict:
    """OpenAI strict mode wants every property required and additionalProperties false."""
    s = schema.model_json_schema()

    def fix(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)
    fix(s)
    return s
