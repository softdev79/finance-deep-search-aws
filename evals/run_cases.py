"""Generate eval runs: every case in cases.jsonl through the full workflow (same step handlers
as the cloud). Writes evals/runs.json mapping case id -> run id. ~2 min and ~$0.02 per case.

    python -m evals.run_cases
"""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from deepsearch import store
from deepsearch.steps import handler

HERE = Path(__file__).parent


def run_case(case: dict) -> tuple[str, str]:
    run_id = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    store.create_run(run_id, case["question"], case["tickers"])
    state = {"run_id": run_id, "attempt": 0, "feedback": ""}
    t0 = time.time()
    try:
        while True:
            out = handler({"step": "plan", **state})
            for wave in out["waves"]:
                with ThreadPoolExecutor(3) as pool:
                    list(pool.map(lambda tid: handler({"step": "task", "run_id": run_id,
                                                       "attempt": state["attempt"], "task_id": tid}), wave))
            val = handler({"step": "validate", "run_id": run_id, "attempt": state["attempt"]})
            if val["decision"] != "replan":
                break
            state = {"run_id": run_id, "attempt": val["attempt"], "feedback": val["feedback"]}
        handler({"step": "synthesize", "run_id": run_id, "attempt": state["attempt"]})
    except Exception as e:
        handler({"step": "fail", "run_id": run_id, "error": {"Cause": f"{type(e).__name__}: {e}"}})
    store.update_run(run_id, seconds=round(time.time() - t0, 1))
    print(f"{case['id']}: {store.get_run(run_id)['status']} in {time.time() - t0:.0f}s", flush=True)
    return case["id"], run_id


if __name__ == "__main__":
    cases = [json.loads(line) for line in (HERE / "cases.jsonl").open(encoding="utf-8")]
    with ThreadPoolExecutor(2) as pool:
        runs = dict(pool.map(run_case, cases))
    (HERE / "runs.json").write_text(json.dumps(runs, indent=1), encoding="utf-8")
    print(json.dumps(runs, indent=1))
