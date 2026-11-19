"""researcher.py - run the plan wave by wave (Section 5.8).

AWS edition:
- tools get tickers from the task (planner fills them), not from a hard-coded name list;
- waves() exposes the wave order so Step Functions can run each wave as a Map state;
- run_task() is a pure "do one task" function the cloud worker calls directly;
- a semaphore caps parallel tool calls (rate limits, Lambda concurrency).
"""
from __future__ import annotations

import asyncio

from deepsearch import llm
from deepsearch.common import ResearchPlan, SubTask, TaskStatus
from deepsearch.tools.financial_data import get_financial_metrics
from deepsearch.tools.sec_filings import get_risk_factors
from deepsearch.tools.web_search import search_financial_news

MAX_PARALLEL = 3
MAX_CONTEXT_CHARS = 24_000  # per thinking task (Section 12: keep the desk clear)


def waves(plan: ResearchPlan) -> list[list[int]]:
    """Topological layers: tasks whose dependencies are all in earlier waves."""
    done: set[int] = set()
    pending = {t.id: t for t in plan.sub_tasks}
    out: list[list[int]] = []
    while pending:
        ready = [tid for tid, t in pending.items() if all(d in done for d in t.dependencies)]
        if not ready:  # cycle: let the validator report the stuck tasks as gaps
            break
        out.append(sorted(ready))
        done.update(ready)
        for tid in ready:
            pending.pop(tid)
    return out


async def analyze_with_llm(instruction: str, context: str) -> str:
    """For 'thinking' tasks: reason over earlier results only."""
    return await llm.complete([
        {"role": "system", "content": "You are a financial analyst. Use ONLY the data given. "
         "Quote numbers exactly, with units. If something is missing, say so."},
        {"role": "user", "content": f"{instruction}\n\nDATA:\n{context[:MAX_CONTEXT_CHARS]}"},
    ], reasoning_effort="low")  # comparisons over given data; hidden reasoning tokens add cost, not quality


async def run_task(task: SubTask, results: dict[int, str]) -> tuple[int, str]:
    """Run one task with the right tool. Never raises: failures become FAILED + 'ERROR: ...'."""
    task.status = TaskStatus.RUNNING
    try:
        if not task.data_sources:                                   # thinking task
            context = "\n\n".join(f"[Task {d}] {results.get(d, 'MISSING')}" for d in task.dependencies)
            output = await analyze_with_llm(task.description, context)
        elif task.data_sources[0] == "financial_api":
            output = (await get_financial_metrics(task.tickers[0])).model_dump_json()
        elif task.data_sources[0] == "sec_filing":
            parts = await asyncio.gather(*[get_risk_factors(t) for t in task.tickers], return_exceptions=True)
            output = "\n\n".join(p if isinstance(p, str) else f"{tk}: ERROR {p}"
                                 for tk, p in zip(task.tickers, parts))
            if all(not isinstance(p, str) for p in parts):
                raise RuntimeError(output)
        else:                                                       # web_search
            output = await search_financial_news(task.description)
        task.status = TaskStatus.COMPLETED
        task.error = None
    except Exception as e:  # never crash the run; the validator turns this into a gap
        task.status = TaskStatus.FAILED
        task.error = f"{type(e).__name__}: {e}"[:500]
        output = f"ERROR: {task.error}"
    return task.id, output


async def execute_plan(plan: ResearchPlan) -> dict[int, str]:
    """Local runner: the same waves the cloud runs, with asyncio instead of Step Functions."""
    results: dict[int, str] = {}
    tasks = {t.id: t for t in plan.sub_tasks}
    gate = asyncio.Semaphore(MAX_PARALLEL)

    async def one(t: SubTask):
        async with gate:
            return await run_task(t, results)

    for wave in waves(plan):
        ready = [tasks[i] for i in wave if all(tasks[d].status == TaskStatus.COMPLETED
                                                for d in tasks[i].dependencies)]
        for tid, out in await asyncio.gather(*[one(t) for t in ready]):
            results[tid] = out
    return results
