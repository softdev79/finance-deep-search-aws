"""api.py - the public API + UI Lambda (Function URL). Starts runs and reports their progress.

    GET  /                  the research page
    GET  /api/health        liveness
    POST /api/runs          {question, tickers} -> starts the Step Functions execution, returns run_id
    GET  /api/runs          recent runs
    GET  /api/runs/{id}     run record (plan, task states, attempts, cost) + memo when done
All /api/* except health need X-API-Key (stored in SSM).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import FileResponse
from fastapi.security.api_key import APIKeyHeader
from mangum import Mangum
from pydantic import BaseModel, Field, field_validator

from deepsearch import store

STATE_MACHINE_ARN = os.getenv("STATE_MACHINE_ARN", "")
MAX_TICKERS = 3
UI = Path(__file__).resolve().parent / "static" / "index.html"

app = FastAPI(title="Finance Deep Search")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


@lru_cache(maxsize=1)
def _api_key() -> str:
    if os.getenv("API_KEY"):
        return os.environ["API_KEY"]
    import boto3

    return boto3.client("ssm").get_parameter(Name=os.environ["API_KEY_PARAM"], WithDecryption=True)["Parameter"]["Value"]


def verify(key: str = Security(api_key_header)) -> None:
    if not secrets.compare_digest(key, _api_key()):
        raise HTTPException(403, "Invalid API key")


class NewRun(BaseModel):
    question: str = Field(min_length=10, max_length=1000)
    tickers: list[str] = Field(min_length=1, max_length=MAX_TICKERS)

    @field_validator("tickers")
    @classmethod
    def clean(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = t.strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z.\-]{0,6}", t):
                raise ValueError(f"not a ticker: {t!r}")
            if t not in out:
                out.append(t)
        return out


@app.get("/", include_in_schema=False)
def page():
    return FileResponse(UI, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    return {"status": "ok", "workflow": bool(STATE_MACHINE_ARN)}


@app.post("/api/runs", dependencies=[Depends(verify)])
def start_run(body: NewRun):
    import boto3

    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, body.question, body.tickers)
    boto3.client("stepfunctions").start_execution(
        stateMachineArn=STATE_MACHINE_ARN, name=run_id,
        input=json.dumps({"run_id": run_id, "attempt": 0, "feedback": ""}))
    return {"run_id": run_id}


@app.get("/api/runs", dependencies=[Depends(verify)])
def runs():
    return store.list_runs(20)


@app.get("/api/runs/{run_id}", dependencies=[Depends(verify)])
def run(run_id: str):
    if not re.fullmatch(r"[a-z0-9\-]{6,40}", run_id):
        raise HTTPException(400, "bad run id")
    rec = store.get_run(run_id)
    if not rec:
        raise HTTPException(404, "run not found")
    if rec.get("status") == "done":
        rec["memo_md"] = store.get_artifact(run_id, "memo.md")
    return rec


handler = Mangum(app, lifespan="off")
