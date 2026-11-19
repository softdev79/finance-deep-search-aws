"""
score.py - evaluation harness + release gate for the deep search agent (Section 11 pattern:
dataset -> pipeline -> metrics -> scorecard -> release gate).

Scores the saved runs listed in evals/runs.json (generate them with `python -m evals.run_cases`).
Most checks are plain code against the run's own task results, so they are cheap, repeatable and
can run in CI without calling any API:

  completeness       every ticker has a metrics row and a risk bullet; all memo sections present
  factual_accuracy   every metric in the table matches the tool's CompanyMetrics within 0.5%
  no_invented_values P/E and market cap say n/a when the tool had no value
  number_grounding   share of % figures in the memo prose that appear in the findings
  citations          every source URL in the memo exists in the findings; every 10-K used is cited
  run_ok             run finished; cost and latency under budget
  judge (optional)   LLM-as-judge 1-5 on structure + reasoning (--judge; uses Bedrock)

Scorecard -> SHIP (exit 0) / CONDITIONAL (exit 1) / BLOCK (exit 2), plus docs/evidence/eval_report.md.
Fault switches prove the gate works:  --fault drop_ticker | wrong_units | fake_source | invent_pe

    python -m evals.score                 # code checks only (CI)
    python -m evals.score --judge         # + LLM judge
    python -m evals.score --fault wrong_units ; echo $?   # -> 2 (BLOCK)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from deepsearch import store
from deepsearch.common import CompanyMetrics, ResearchPlan, ResearchReport

HERE = Path(__file__).parent
REPORT = HERE.parent / "docs" / "evidence" / "eval_report.md"
URL = re.compile(r"https?://[^\s)\]>'\"|;]+")
TOL = 0.005

# metric, threshold, comparison, severity if failed
THRESHOLDS = [
    ("run_ok", 1.0, ">=", "BLOCK"),
    ("completeness", 0.95, ">=", "BLOCK"),
    ("factual_accuracy", 1.0, ">=", "BLOCK"),
    ("no_invented_values", 1.0, ">=", "BLOCK"),
    ("citations_valid", 1.0, ">=", "BLOCK"),
    ("tenk_cited", 0.9, ">=", "CONDITIONAL"),
    ("number_grounding", 0.85, ">=", "CONDITIONAL"),
    ("avg_cost_usd", 0.05, "<=", "CONDITIONAL"),
    ("p95_seconds", 300, "<=", "CONDITIONAL"),
    ("judge_avg", 3.5, ">=", "CONDITIONAL"),
]


# ---------------------------------------------------------------- loading
def load_run(run_id: str) -> dict | None:
    run = store.get_run(run_id)
    if not run or run.get("status") != "done":
        return {"run": run or {"run_id": run_id}, "ok": False}
    attempt = run.get("attempt", 0)
    plan = ResearchPlan.model_validate_json(store.get_artifact(run_id, f"attempt-{attempt}/plan.json"))
    results = {t.id: store.get_artifact(run_id, f"attempt-{attempt}/task-{t.id}.txt") or "" for t in plan.sub_tasks}
    report = ResearchReport.model_validate_json(store.get_artifact(run_id, "report.json"))
    metrics = {}
    for text in results.values():
        try:
            m = CompanyMetrics.model_validate_json(text)
            metrics[m.ticker] = m
        except ValueError:
            pass
    return {"run": run, "ok": True, "plan": plan, "results": results, "report": report, "metrics": metrics}


# ---------------------------------------------------------------- faults
def apply_fault(case: dict, fault: str) -> None:
    r: ResearchReport = case["report"]
    rows = r.key_metrics_table.splitlines()
    if fault == "drop_ticker":
        tk = next(iter(case["metrics"]))
        r.key_metrics_table = "\n".join(x for x in rows if tk not in x)
        r.risk_factors = [x for x in r.risk_factors if tk not in x]
    elif fault == "wrong_units":   # revenue reported in $ thousands instead of $ millions
        m = next(iter(case["metrics"].values()))
        rev = _fmt_variants(m.revenue)[0]
        r.key_metrics_table = r.key_metrics_table.replace(rev, f"{m.revenue * 1000:,.1f}", 1)
    elif fault == "fake_source":
        r.sources = r.sources + ["https://www.example-research.com/made-up-report"]
    elif fault == "invent_pe":
        r.key_metrics_table = re.sub(r"\bn/?a\b", "35.2", r.key_metrics_table, count=1, flags=re.I)


# ---------------------------------------------------------------- checks
MINUS = str.maketrans({"‑": "-", "‐": "-", "−": "-", "–": "-", "—": "-"})


def _numbers(text: str) -> list[float]:
    text = text.translate(MINUS)  # models write non-breaking hyphens and minus signs
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", text)]


def _fmt_variants(v: float) -> list[str]:
    return [f"{v:,.1f}", f"{v:,.0f}", f"{v:.1f}", f"{v}"]


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(abs(b) * TOL, 0.011)


def _row(table: str, ticker: str) -> str | None:
    for line in table.splitlines():
        if line.strip().startswith("|") and re.search(rf"\b{re.escape(ticker)}\b", line):
            return line
    return None


def check(case: dict) -> dict:
    r: ResearchReport = case["report"]
    tickers = case["run"]["tickers"]
    out = {}

    # completeness
    sections = [r.executive_summary, r.key_metrics_table, r.competitive_analysis, r.conclusion, r.risk_factors, r.sources]
    rows = [_row(r.key_metrics_table, t) for t in tickers]
    risk_cov = [any(t in x or _name_hint(t).lower() in x.lower() for x in r.risk_factors + [r.competitive_analysis])
                for t in tickers]
    out["completeness"] = (sum(bool(s) for s in sections) + sum(bool(x) for x in rows) + sum(risk_cov)) / (
        len(sections) + 2 * len(tickers))

    # factual accuracy + no invented values (table row vs tool output)
    checked = correct = invented = 0
    for t, row in zip(tickers, rows):
        m = case["metrics"].get(t)
        if not m or not row:
            continue
        cells = [c.strip() for c in row.strip().strip("|").split("|")][1:]
        nums = [n for c in cells for n in _numbers(c)]
        for v in (m.revenue, m.revenue_growth, m.gross_margin, m.operating_margin, m.eps):
            if v is None:
                continue
            checked += 1
            correct += any(_close(n, v) for n in nums)
        if m.pe_ratio is None and m.market_cap is None:
            tail = " ".join(cells[-2:])  # the P/E and market-cap columns
            invented += bool(_numbers(tail))
    out["factual_accuracy"] = correct / checked if checked else 0.0
    out["no_invented_values"] = 0.0 if invented else 1.0

    # number grounding: % figures in prose must exist in the findings
    findings = "\n".join(case["results"].values())
    found_nums = _numbers(findings)
    prose = " ".join([r.executive_summary, r.competitive_analysis, r.conclusion] + r.risk_factors)
    pcts = [float(x) for x in re.findall(r"(-?\d+(?:\.\d+)?)\s?%", prose)]
    out["number_grounding"] = (sum(any(_close(p, f) for f in found_nums) for p in pcts) / len(pcts)) if pcts else 1.0

    # citations
    finding_urls = {u.rstrip(".,;") for u in URL.findall(findings)}
    memo_urls = {u.rstrip(".,;") for u in URL.findall(" ".join(r.sources))}
    out["citations_valid"] = 1.0 if all(any(u.startswith(f) or f.startswith(u) for f in finding_urls) for u in memo_urls) else 0.0
    tenks = {u for u in finding_urls if "/Archives/edgar/" in u}
    out["tenk_cited"] = (len(tenks & memo_urls) / len(tenks)) if tenks else 1.0
    return out


def _name_hint(ticker: str) -> str:
    """First word of the SEC-registered company name (Apple Inc. -> Apple), for risk-bullet matching."""
    from deepsearch.tools.sec_filings import company_name

    name = asyncio.run(company_name(ticker)) or ticker
    return name.split()[0].strip(",.")


async def judge(case: dict) -> float:
    from deepsearch import llm
    from pydantic import BaseModel, Field

    class Verdict(BaseModel):
        structure: int = Field(ge=1, le=5)
        reasoning: int = Field(ge=1, le=5)
        reason: str

    r: ResearchReport = case["report"]
    memo = json.dumps(r.model_dump(), indent=1)[:12000]
    v = await llm.parse([
        {"role": "system", "content": "You grade equity research memos. structure: are all sections clear, "
         "scannable and on-topic for the question (1-5)? reasoning: are the conclusions supported by the "
         "numbers and risks quoted, with caveats where data is missing (1-5)? Be strict; 5 is rare."},
        {"role": "user", "content": f"QUESTION: {case['run']['question']}\n\nMEMO:\n{memo}"}], Verdict)
    return (v.structure + v.reasoning) / 2


# ---------------------------------------------------------------- scorecard
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--fault", choices=["drop_ticker", "wrong_units", "fake_source", "invent_pe"])
    args = ap.parse_args()

    runs = json.loads((HERE / "runs.json").read_text(encoding="utf-8"))
    rows, per_case = {}, []
    for case_id, run_id in runs.items():
        case = load_run(run_id)
        res = {"case": case_id, "run_ok": 1.0 if case["ok"] else 0.0}
        if case["ok"]:
            if args.fault:
                apply_fault(case, args.fault)
            res.update(check(case))
            res["cost"] = float(case["run"].get("cost_usd", 0))
            res["seconds"] = float(case["run"].get("seconds", 0))
            if args.judge:
                res["judge"] = asyncio.run(judge(case))
        per_case.append(res)

    def avg(k):
        vals = [c[k] for c in per_case if k in c]
        return sum(vals) / len(vals) if vals else None

    secs = sorted(c["seconds"] for c in per_case if "seconds" in c)
    rows = {k: avg(k) for k in ["run_ok", "completeness", "factual_accuracy", "no_invented_values",
                                "number_grounding", "citations_valid", "tenk_cited"]}
    rows["avg_cost_usd"] = avg("cost")
    rows["p95_seconds"] = secs[min(len(secs) - 1, int(0.95 * len(secs)))] if secs else None
    rows["judge_avg"] = avg("judge")

    verdict, lines = "SHIP", []
    for metric, thr, op, sev in THRESHOLDS:
        val = rows.get(metric)
        if val is None:
            lines.append(f"| {metric} | n/a | {op} {thr} | skipped |")
            continue
        ok = val >= thr if op == ">=" else val <= thr
        if not ok:
            verdict = "BLOCK" if sev == "BLOCK" or verdict == "BLOCK" else "CONDITIONAL"
        lines.append(f"| {metric} | {val:.3f} | {op} {thr} | {'pass' if ok else 'FAIL (' + sev + ')'} |")

    title = f"# Eval scorecard: {verdict}" + (f" (fault injected: {args.fault})" if args.fault else "")
    detail = ["| case | " + " | ".join(k for k in per_case[0] if k != "case") + " |",
              "|---" * (len(per_case[0])) + "|"]
    for c in per_case:
        detail.append(f"| {c['case']} | " + " | ".join(f"{v:.3f}" if isinstance(v, float) else str(v)
                                                       for k, v in c.items() if k != "case") + " |")
    report = "\n".join([title, "", f"{len(per_case)} cases.", "", "| metric | value | threshold | result |",
                        "|---|---|---|---|", *lines, "", "## Per case", "", *detail, ""])
    print(report)
    if not args.fault:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(report, encoding="utf-8")
    return {"SHIP": 0, "CONDITIONAL": 1, "BLOCK": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
