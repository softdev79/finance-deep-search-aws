"""steps.py - the deep search loop split into Step Functions states.

One worker Lambda handles every state; `event["step"]` picks the function:

    plan        -> ResearchPlan saved, returns {"waves": [[1,2,3],[4],[5]]}
    task        -> runs ONE sub-task (called in parallel by an inner Map state)
    validate    -> plain-code checks; decides "replan" (max 2) or "synthesize"
    synthesize  -> the memo (JSON + Markdown) and the final run record
    fail        -> marks the run failed (Catch on every state)

Only IDs travel through Step Functions (256 KB payload limit); everything else is read from and
written to the store (DynamoDB run record + S3 artefacts). The same functions run locally
(scripts/simulate_workflow.py) with a file-based store.
"""
from __future__ import annotations

import asyncio
import json
import time

from deepsearch import llm, store
from deepsearch.common import ResearchPlan, TaskStatus, ValidationResult
from deepsearch.planner import create_research_plan
from deepsearch.researcher import run_task, waves
from deepsearch.synthesizer import synthesize_report, to_markdown
from deepsearch.validator import validate_research

MAX_REPLAN_ATTEMPTS = 2


def _record_usage(run_id: str, before: tuple[int, int, float]) -> None:
    u = llm.USAGE
    store.add_usage(run_id, u.input_tokens - before[0], u.output_tokens - before[1], u.cost_usd - before[2])


def _load_plan(run_id: str, attempt: int) -> ResearchPlan:
    return ResearchPlan.model_validate_json(store.get_artifact(run_id, f"attempt-{attempt}/plan.json"))


def _load_results(run_id: str, attempt: int, plan: ResearchPlan) -> dict[int, str]:
    out = {}
    for t in plan.sub_tasks:
        text = store.get_artifact(run_id, f"attempt-{attempt}/task-{t.id}.txt")
        if text is not None:
            out[t.id] = text
    return out


def _apply_statuses(run_id: str, plan: ResearchPlan) -> None:
    tasks = (store.get_run(run_id) or {}).get("tasks", {})
    for t in plan.sub_tasks:
        info = tasks.get(str(t.id), {})
        t.status = TaskStatus(info.get("status", "pending"))
        t.error = info.get("error")


async def step_plan(run_id: str, attempt: int, feedback: str) -> dict:
    run = store.get_run(run_id)
    plan = await create_research_plan(run["question"], run["tickers"], feedback)
    store.put_artifact(run_id, f"attempt-{attempt}/plan.json", plan.model_dump_json(indent=1))
    store.update_run(run_id, status="researching", attempt=attempt, tasks={
        str(t.id): {"description": t.description, "sources": t.data_sources, "tickers": t.tickers,
                    "deps": t.dependencies, "status": "pending", "error": None}
        for t in plan.sub_tasks})
    return {"waves": waves(plan), "task_count": len(plan.sub_tasks)}


async def step_task(run_id: str, attempt: int, task_id: int) -> dict:
    plan = await asyncio.to_thread(_load_plan, run_id, attempt)
    _apply_statuses(run_id, plan)
    task = next(t for t in plan.sub_tasks if t.id == task_id)
    failed_deps = [d for d in task.dependencies
                   if next(t for t in plan.sub_tasks if t.id == d).status != TaskStatus.COMPLETED]
    if failed_deps:  # the local runner skips these too; the validator reports them as gaps
        store.set_task_status(run_id, task_id, "failed", f"dependencies not completed: {failed_deps}")
        return {"task_id": task_id, "status": "failed"}
    store.set_task_status(run_id, task_id, "running")
    results = _load_results(run_id, attempt, plan) if task.dependencies else {}
    t0 = time.time()
    _, output = await run_task(task, results)
    store.put_artifact(run_id, f"attempt-{attempt}/task-{task_id}.txt", output, "text/plain")
    store.set_task_status(run_id, task_id, task.status.value, task.error)
    return {"task_id": task_id, "status": task.status.value, "seconds": round(time.time() - t0, 1)}


async def step_validate(run_id: str, attempt: int) -> dict:
    run = store.get_run(run_id)
    plan = _load_plan(run_id, attempt)
    _apply_statuses(run_id, plan)
    v = validate_research(plan, _load_results(run_id, attempt, plan), run["tickers"])
    store.put_artifact(run_id, f"attempt-{attempt}/validation.json", v.model_dump_json(indent=1))
    history = run.get("attempts", []) + [{"attempt": attempt, **v.model_dump()}]
    replan = not v.is_valid and attempt < MAX_REPLAN_ATTEMPTS
    store.update_run(run_id, attempts=history, status="replanning" if replan else "writing")
    return {"decision": "replan" if replan else "synthesize", "attempt": attempt + 1 if replan else attempt,
            "feedback": "\n".join(v.gaps + v.errors)[:4000]}


async def step_synthesize(run_id: str, attempt: int) -> dict:
    run = store.get_run(run_id)
    plan = _load_plan(run_id, attempt)
    results = _load_results(run_id, attempt, plan)
    v = ValidationResult.model_validate_json(store.get_artifact(run_id, f"attempt-{attempt}/validation.json"))
    report = await synthesize_report(run["question"], results, v)
    store.put_artifact(run_id, "report.json", report.model_dump_json(indent=1))
    store.put_artifact(run_id, "memo.md", to_markdown(report), "text/markdown")
    store.update_run(run_id, status="done", title=report.title, confidence=report.confidence_score,
                     finished_at=int(time.time()))
    return {"status": "done", "confidence": report.confidence_score}


def step_fail(run_id: str, error: dict) -> dict:
    cause = error.get("Cause", "") if isinstance(error, dict) else str(error)
    try:
        cause = json.loads(cause).get("errorMessage", cause)
    except (ValueError, AttributeError):
        pass
    store.update_run(run_id, status="failed", error=str(cause)[:1000], finished_at=int(time.time()))
    return {"status": "failed"}


def handler(event, _context=None):
    """Lambda entry point for every Step Functions state."""
    from deepsearch import secrets

    secrets.load()
    step, run_id = event["step"], event["run_id"]
    attempt = int(event.get("attempt", 0))
    if step == "fail":
        return step_fail(run_id, event.get("error", {}))
    rec = store.get_run(run_id) or {}
    llm.start_run_usage(int(rec.get("input_tokens", 0)) + int(rec.get("output_tokens", 0)))
    before = (0, 0, 0.0)
    try:
        if step == "plan":
            return asyncio.run(step_plan(run_id, attempt, event.get("feedback", "")))
        if step == "task":
            return asyncio.run(step_task(run_id, attempt, int(event["task_id"])))
        if step == "validate":
            return asyncio.run(step_validate(run_id, attempt))
        if step == "synthesize":
            return asyncio.run(step_synthesize(run_id, attempt))
        raise ValueError(f"unknown step {step}")
    finally:
        _record_usage(run_id, before)
