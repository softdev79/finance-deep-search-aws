"""Live smoke test through the public URL: page, health, auth, then one full research run
(Step Functions end to end) polled until done. Stdlib only.

    python scripts/smoke_deploy.py https://xxxx.lambda-url.us-east-2.on.aws/
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ENV = os.path.join(os.path.dirname(__file__), "..", ".env")


def api_key() -> str:
    for line in open(ENV, encoding="utf-8"):
        if line.startswith("API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("API_KEY not in .env")


def call(base, path, key=None, body=None, retries=1):
    for attempt in range(retries):
        req = urllib.request.Request(base + path, method="POST" if body else "GET",
                                     data=json.dumps(body).encode() if body else None,
                                     headers={**({"X-API-Key": key} if key else {}),
                                              **({"Content-Type": "application/json"} if body else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
            if attempt == retries - 1 or status not in (403, 502, 503):
                return status, text
            time.sleep(10)
    return status, text


base, key, ok = sys.argv[1].rstrip("/"), api_key(), True
s, b = call(base, "/", retries=6)
print(f"GET  /               {s}  {'UI ok' if 'Finance Deep Search' in b else b[:100]}"); ok &= s == 200
s, b = call(base, "/api/health", retries=6)
print(f"GET  /api/health     {s}  {b}"); ok &= s == 200
s, _ = call(base, "/api/runs", key="wrong")
print(f"GET  /api/runs       {s}  (wrong key, expect 403)"); ok &= s == 403

q = "Compare NVIDIA with AMD and Intel on revenue growth, profitability and key risk factors."
s, b = call(base, "/api/runs", key, {"question": q, "tickers": ["NVDA", "AMD", "INTC"]})
print(f"POST /api/runs       {s}  {b}"); ok &= s == 200
run_id = json.loads(b)["run_id"] if s == 200 else None
t0, last, r = time.time(), "", {"status": "not started"}
while run_id and time.time() - t0 < 600:
    s, b = call(base, f"/api/runs/{run_id}", key)
    r = json.loads(b)
    tasks = r.get("tasks", {})
    line = f"  {int(time.time() - t0):4d}s {r['status']:<12} " + " ".join(
        f"{k}:{v['status'][0]}" for k, v in sorted(tasks.items(), key=lambda kv: int(kv[0])))
    if line.split(maxsplit=1)[1] != last:
        print(line)
        last = line.split(maxsplit=1)[1]
    if r["status"] in ("done", "failed"):
        break
    time.sleep(5)

print(f"\nrun {run_id}: {r['status']} in {r.get('finished_at', 0) - r.get('created_at', 0)}s, "
      f"{r.get('input_tokens', 0) + r.get('output_tokens', 0):,} tokens, ${float(r.get('cost_usd', 0)):.4f}, "
      f"confidence {r.get('confidence')}, attempts {len(r.get('attempts', []))}")
if r.get("error"):
    print("ERROR:", r["error"])
ok &= r["status"] == "done" and bool(r.get("memo_md"))
if r.get("memo_md"):
    print("\n" + "\n".join(r["memo_md"].splitlines()[:16]))
print("\nSMOKE TEST PASSED" if ok else "\nSMOKE TEST FAILED")
sys.exit(0 if ok else 1)
