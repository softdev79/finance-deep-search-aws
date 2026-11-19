"""store.py - where a research run lives between Step Functions states.

- Run record (status, plan + per-task status, attempts, tokens, cost, confidence) ->
  DynamoDB table RUNS_TABLE on AWS, runs/<run_id>/run.json locally. The UI polls this.
- Artefacts (plan, every task result, validation, memo) -> S3 bucket ARTIFACTS_BUCKET on AWS,
  runs/<run_id>/... locally. Section 12: "store everything, durably" so any run can be replayed.
"""
from __future__ import annotations

import json
import os
import threading
import time
from decimal import Decimal
from pathlib import Path

RUNS_TABLE = os.getenv("RUNS_TABLE", "")
BUCKET = os.getenv("ARTIFACTS_BUCKET", "")
LOCAL = Path(os.getenv("LOCAL_RUNS_DIR", str(Path(__file__).resolve().parent.parent / "runs")))
RUN_TTL_DAYS = 90
_LOCAL_LOCK = threading.RLock()  # local store: parallel task threads must not overwrite each other


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(RUNS_TABLE)


def _s3():
    import boto3

    return boto3.client("s3")


def _plain(o):
    """DynamoDB Decimals -> int/float for JSON."""
    if isinstance(o, list):
        return [_plain(x) for x in o]
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, Decimal):
        return int(o) if o == int(o) else float(o)
    return o


def _ddb(o):
    return json.loads(json.dumps(o), parse_float=Decimal)


# ---------------------------------------------------------------- run record
def create_run(run_id: str, question: str, tickers: list[str]) -> dict:
    now = int(time.time())
    rec = {"run_id": run_id, "question": question, "tickers": tickers, "status": "planning",
           "attempt": 0, "created_at": now, "updated_at": now, "tasks": {}, "attempts": [],
           "input_tokens": 0, "output_tokens": 0, "cost_usd": 0, "expires_at": now + RUN_TTL_DAYS * 86400}
    put_run(rec)
    return rec


def get_run(run_id: str) -> dict | None:
    if RUNS_TABLE:
        item = _table().get_item(Key={"run_id": run_id}).get("Item")
        return _plain(item) if item else None
    f = LOCAL / run_id / "run.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def put_run(rec: dict) -> None:
    rec["updated_at"] = int(time.time())
    if RUNS_TABLE:
        _table().put_item(Item=_ddb(rec))
        return
    with _LOCAL_LOCK:
        (LOCAL / rec["run_id"]).mkdir(parents=True, exist_ok=True)
        tmp = LOCAL / rec["run_id"] / "run.json.tmp"
        tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        os.replace(tmp, LOCAL / rec["run_id"] / "run.json")  # atomic: readers never see half a file


def update_run(run_id: str, **fields) -> None:
    if RUNS_TABLE:
        fields["updated_at"] = int(time.time())
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": v for k, v in _ddb(fields).items()}
        _table().update_item(Key={"run_id": run_id},
                             UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
                             ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        return
    with _LOCAL_LOCK:
        rec = get_run(run_id)
        rec.update(fields)
        put_run(rec)


def set_task_status(run_id: str, task_id: int, status: str, error: str | None = None) -> None:
    """Atomic per-task update, so parallel task Lambdas never overwrite each other."""
    if RUNS_TABLE:
        _table().update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #t.#id.#s = :s, #t.#id.#e = :e, updated_at = :now",
            ExpressionAttributeNames={"#t": "tasks", "#id": str(task_id), "#s": "status", "#e": "error"},
            ExpressionAttributeValues={":s": status, ":e": error, ":now": int(time.time())})
        return
    with _LOCAL_LOCK:
        rec = get_run(run_id)
        rec["tasks"][str(task_id)].update(status=status, error=error)
        put_run(rec)


def add_usage(run_id: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    if not (input_tokens or output_tokens):
        return
    if RUNS_TABLE:
        _table().update_item(
            Key={"run_id": run_id},
            UpdateExpression="ADD input_tokens :i, output_tokens :o, cost_usd :c",
            ExpressionAttributeValues={":i": input_tokens, ":o": output_tokens,
                                       ":c": Decimal(str(round(cost_usd, 6)))})
        return
    with _LOCAL_LOCK:
        rec = get_run(run_id)
        rec["input_tokens"] += input_tokens
        rec["output_tokens"] += output_tokens
        rec["cost_usd"] = round(rec["cost_usd"] + cost_usd, 6)
        put_run(rec)


def list_runs(limit: int = 20) -> list[dict]:
    keep = ["run_id", "question", "tickers", "status", "created_at", "confidence", "cost_usd"]
    if RUNS_TABLE:
        items = _table().scan(ProjectionExpression=", ".join(f"#{k}" for k in keep),
                              ExpressionAttributeNames={f"#{k}": k for k in keep})["Items"]
    else:
        items = [json.loads(f.read_text(encoding="utf-8")) for f in LOCAL.glob("*/run.json")]
    items = [{k: v for k, v in _plain(i).items() if k in keep} for i in items]
    return sorted(items, key=lambda r: r.get("created_at", 0), reverse=True)[:limit]


# ---------------------------------------------------------------- artefacts
def put_artifact(run_id: str, name: str, body: str, content_type: str = "application/json") -> None:
    if BUCKET:
        _s3().put_object(Bucket=BUCKET, Key=f"runs/{run_id}/{name}", Body=body.encode("utf-8"),
                         ContentType=content_type)
        return
    f = LOCAL / run_id / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")


def get_artifact(run_id: str, name: str) -> str | None:
    if BUCKET:
        try:
            return _s3().get_object(Bucket=BUCKET, Key=f"runs/{run_id}/{name}")["Body"].read().decode("utf-8")
        except _s3().exceptions.NoSuchKey:
            return None
    f = LOCAL / run_id / name
    return f.read_text(encoding="utf-8") if f.exists() else None
