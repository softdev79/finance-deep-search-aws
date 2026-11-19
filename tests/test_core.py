"""Offline tests: plan clean-up, wave order, validator rules, task error handling, 10-K parsing."""
import asyncio

from deepsearch import researcher
from deepsearch.common import CompanyMetrics, ResearchPlan, SubTask, TaskStatus
from deepsearch.planner import normalize_plan
from deepsearch.researcher import run_task, waves
from deepsearch.tools.sec_filings import _risk_section
from deepsearch.validator import validate_research

T = ["NVDA", "AMD", "INTC"]


def plan(*tasks):
    return ResearchPlan(question="q", sub_tasks=[SubTask(**t) for t in tasks])


def test_normalize_fixes_a_sloppy_plan():
    p = normalize_plan(plan(
        {"id": 1, "description": "metrics for all", "data_sources": ["financial_api", "web_search"],
         "tickers": ["nvda", "amd"], "dependencies": [9]},
        {"id": 2, "description": "risks", "data_sources": ["sec_filing"], "tickers": ["TSLA"]},
        {"id": 3, "description": "news", "data_sources": ["made_up_tool", "web_search"]},
        {"id": 4, "description": "compare", "data_sources": [], "dependencies": [1, 4]},
    ), T)
    fin = [t for t in p.sub_tasks if t.data_sources == ["financial_api"]]
    assert sorted(tk for t in fin for tk in t.tickers) == ["AMD", "INTC", "NVDA"]   # split + added INTC
    assert all(len(t.tickers) == 1 and t.dependencies == [] for t in fin)
    assert p.sub_tasks[1].tickers == sorted(p.sub_tasks[1].tickers) or set(p.sub_tasks[1].tickers) == set(T)
    assert all(len(t.data_sources) <= 1 for t in p.sub_tasks)
    final = p.sub_tasks[-1]
    assert final.data_sources == [] and set(final.dependencies) == {t.id for t in p.sub_tasks[:-1]}
    assert all(t.id not in t.dependencies for t in p.sub_tasks)


def test_waves_follow_dependencies():
    p = plan({"id": 1, "description": "a", "data_sources": ["financial_api"]},
             {"id": 2, "description": "b", "data_sources": ["web_search"]},
             {"id": 3, "description": "c", "data_sources": [], "dependencies": [1]},
             {"id": 4, "description": "d", "data_sources": [], "dependencies": [2, 3]})
    assert waves(p) == [[1, 2], [3], [4]]


def test_validator_catches_gaps_errors_and_warnings():
    p = plan({"id": 1, "description": "nvda", "data_sources": ["financial_api"], "status": "completed"},
             {"id": 2, "description": "amd", "data_sources": ["financial_api"], "status": "failed",
              "error": "HTTPStatusError: 429"})
    bad = CompanyMetrics(ticker="NVDA", gross_margin=40, operating_margin=55, pe_ratio=400, revenue_growth=900)
    v = validate_research(p, {1: bad.model_dump_json(), 2: "ERROR: 429"}, ["NVDA", "AMD"])
    assert not v.is_valid
    assert any("429" in g for g in v.gaps) and "Missing data for AMD" in v.gaps
    assert any("operating margin is above gross margin" in e for e in v.errors)
    assert len(v.warnings) == 2


def test_run_task_never_raises(monkeypatch):
    async def boom(ticker):
        raise RuntimeError("API down")
    monkeypatch.setattr(researcher, "get_financial_metrics", boom)
    t = SubTask(id=1, description="x", data_sources=["financial_api"], tickers=["NVDA"])
    tid, out = asyncio.run(run_task(t, {}))
    assert tid == 1 and out.startswith("ERROR") and t.status == TaskStatus.FAILED and "API down" in t.error


def test_risk_section_skips_table_of_contents():
    body = "x " * 3000
    html = ("<p>Item 1A. Risk Factors ..... 12</p><p>Item 1B. Unresolved ..... 30</p>"
            f"<p>Item 1A. Risk Factors</p><p>Supply chain risk. {body}</p><p>Item 1B. Unresolved Staff Comments</p>")
    s = _risk_section(html)
    assert s.startswith("Item 1A. Risk Factors Supply chain risk") and len(s) > 2000


def test_llm_json_extraction_tolerates_model_noise():
    from deepsearch.llm import _validate
    good = '{"question": "q", "sub_tasks": [{"id": 1, "description": "a\nb", "data_sources": []}]}'
    for text in [good, "**" + good, "##" + good + "\n```", '{\n  "{\n' + good]:
        assert _validate(ResearchPlan, text).sub_tasks[0].id == 1


def test_risk_section_fallback_without_item_1a():
    body = "Our foundry plans may fail. " * 300
    html = (f"<p>Contents Risk Factors 37 Other Key Information</p><p>Risk Factors The following summarizes "
            f"the material factors. {body}</p><p>Other Key Information Executive officers</p>")
    s = _risk_section(html)
    assert s.startswith("Risk Factors The following") and "Executive officers" not in s


def test_validator_warns_on_partial_task_data():
    p = plan({"id": 1, "description": "risks", "data_sources": ["sec_filing"], "status": "completed"})
    v = validate_research(p, {1: "NVDA risk factors: ok\n\nINTC: ERROR could not locate"}, ["NVDA"])
    assert v.is_valid and any("partial data" in w for w in v.warnings)


def test_local_store_parallel_task_updates(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from deepsearch import store
    monkeypatch.setattr(store, "LOCAL", tmp_path)
    monkeypatch.setattr(store, "RUNS_TABLE", "")
    store.create_run("r1", "q", ["A"])
    store.update_run("r1", tasks={str(i): {"status": "pending", "error": None} for i in range(1, 21)})
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: store.set_task_status("r1", i, "completed"), range(1, 21)))
    assert all(t["status"] == "completed" for t in store.get_run("r1")["tasks"].values())


def test_token_budget_is_per_run(monkeypatch):
    from deepsearch import llm
    monkeypatch.setenv("RUN_MAX_TOKENS", "1000")
    class U: prompt_tokens, completion_tokens = 400, 400
    llm.start_run_usage(); llm.USAGE.add("m", U())          # run 1 uses 800
    llm.start_run_usage(); llm.USAGE.add("m", U())          # run 2 starts fresh: no error
    llm.start_run_usage(already_used_tokens=500)            # a later state of a run that used 500
    import pytest
    with pytest.raises(RuntimeError):
        llm.USAGE.add("m", U())
