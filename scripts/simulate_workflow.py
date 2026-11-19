"""Run the Step Functions workflow locally: the same states, in the same order, against the
file-based store in runs/. Proves the cloud step handlers before deploying.

    python -m scripts.simulate_workflow                      # course question
    python -m scripts.simulate_workflow "question" AAPL MSFT
"""
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from deep_search_agent import COURSE_QUESTION
from deepsearch import store
from deepsearch.steps import handler

q, tickers = (sys.argv[1], [t.upper() for t in sys.argv[2:]]) if len(sys.argv) > 2 else (COURSE_QUESTION, ["NVDA", "AMD", "INTC"])
run_id = uuid.uuid4().hex[:12]
store.create_run(run_id, q, tickers)
t0 = time.time()
state = {"run_id": run_id, "attempt": 0, "feedback": ""}
try:
    while True:
        out = handler({"step": "plan", **state})
        print(f"plan (attempt {state['attempt']}): waves {out['waves']}")
        for wave in out["waves"]:                                  # outer Map, MaxConcurrency 1
            with ThreadPoolExecutor(3) as pool:                    # inner Map, MaxConcurrency 3
                res = list(pool.map(lambda tid: handler({"step": "task", "run_id": run_id,
                                                         "attempt": state["attempt"], "task_id": tid}), wave))
            print("  wave", [(r["task_id"], r["status"]) for r in res])
        val = handler({"step": "validate", "run_id": run_id, "attempt": state["attempt"]})
        print(f"validate: {val['decision']}")
        if val["decision"] != "replan":
            break
        state = {"run_id": run_id, "attempt": val["attempt"], "feedback": val["feedback"]}
    print("synthesize:", handler({"step": "synthesize", "run_id": run_id, "attempt": state["attempt"]}))
except Exception as e:
    handler({"step": "fail", "run_id": run_id, "error": {"Cause": str(e)}})
    raise
run = store.get_run(run_id)
print(f"\nrun {run_id}: {run['status']} in {time.time() - t0:.0f}s, {run['input_tokens']}+{run['output_tokens']} tokens, "
      f"${run['cost_usd']:.4f}, confidence {run.get('confidence')}")
print(f"memo: runs/{run_id}/memo.md")
