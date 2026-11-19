"""deep_search_agent.py - run the whole loop locally (Section 5.10): plan -> waves -> validate ->
re-plan (max 2) -> synthesize. The cloud runs the SAME functions as Step Functions states.

    python deep_search_agent.py                       # the course question (NVDA vs AMD vs INTC)
    python deep_search_agent.py "Your question" AAPL MSFT
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from deepsearch import llm
from deepsearch.common import ResearchReport
from deepsearch.planner import create_research_plan
from deepsearch.researcher import execute_plan, waves
from deepsearch.synthesizer import synthesize_report, to_markdown
from deepsearch.validator import validate_research

MAX_REPLAN_ATTEMPTS = 2
COURSE_QUESTION = ("Analyze NVIDIA's financial performance and competitive position in the AI chip market. "
                   "Compare with AMD and Intel on revenue growth, profitability, and key risk factors. "
                   "Produce a structured investment research memo.")


async def deep_search(question: str, tickers: list[str], log=print) -> tuple[ResearchReport, dict]:
    llm.start_run_usage()
    feedback, trace = "", {"attempts": []}
    for attempt in range(MAX_REPLAN_ATTEMPTS + 1):
        t0 = time.time()
        plan = await create_research_plan(question, tickers, feedback)
        log(f"[attempt {attempt + 1}] plan: {len(plan.sub_tasks)} tasks in waves {waves(plan)}")
        results = await execute_plan(plan)
        validation = validate_research(plan, results, tickers)
        log(f"[attempt {attempt + 1}] valid={validation.is_valid} errors={validation.errors} "
            f"gaps={validation.gaps} warnings={validation.warnings} ({time.time() - t0:.1f}s)")
        trace["attempts"].append({"plan": plan.model_dump(mode="json"), "results": results,
                                  "validation": validation.model_dump()})
        if validation.is_valid:
            break
        feedback = "\n".join(validation.gaps + validation.errors)
    report = await synthesize_report(question, results, validation)
    trace["usage"] = {"calls": llm.USAGE.calls, "input_tokens": llm.USAGE.input_tokens,
                      "output_tokens": llm.USAGE.output_tokens, "cost_usd": round(llm.USAGE.cost_usd, 4)}
    return report, trace


if __name__ == "__main__":
    q, tks = (sys.argv[1], [t.upper() for t in sys.argv[2:]]) if len(sys.argv) > 2 else (COURSE_QUESTION, ["NVDA", "AMD", "INTC"])
    t0 = time.time()
    report, trace = asyncio.run(deep_search(q, tks))
    out = Path("output")
    out.mkdir(exist_ok=True)
    (out / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (out / "report.md").write_text(to_markdown(report), encoding="utf-8")
    (out / "trace.json").write_text(json.dumps(trace, indent=1, default=str), encoding="utf-8")
    print(f"\n{report.title}\n{report.executive_summary}\nConfidence: {report.confidence_score:.0%}")
    print(f"Total {time.time() - t0:.0f}s, {trace['usage']}")
