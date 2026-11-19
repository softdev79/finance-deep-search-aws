"""planner.py - turns ONE question into a to-do list (a ResearchPlan).

From Section 5.6. AWS edition: Bedrock model via llm.parse(); the tickers are passed in and the
prompt makes each data task use exactly one tool and one or more tickers, so tools never have to
guess companies from free text. normalize_plan() then fixes anything the model still gets wrong,
in code (ids, unknown sources, dangling dependencies, missing synthesis task).
"""
from __future__ import annotations

from deepsearch import llm
from deepsearch.common import ResearchPlan, SubTask

SOURCES = {"financial_api", "sec_filing", "web_search"}

PLANNER_PROMPT = """You are a financial research planner. Given a research question and a list of
company tickers, decompose the question into concrete sub-tasks that can be executed by tools.

Available data sources (tools):
- financial_api: structured metrics for ONE company (revenue, growth, EPS, margins, P/E, market cap)
- sec_filing: risk factors from the latest 10-K annual report (one or more companies)
- web_search: recent news, competitive intelligence, analyst commentary

Rules:
1. Data-gathering tasks use EXACTLY ONE data source and have NO dependencies.
2. Create one financial_api task PER company. Put the company's ticker in `tickers`.
3. Use one sec_filing task covering all companies, and one or two web_search tasks.
4. Analysis tasks have an EMPTY data_sources list and depend on the data tasks they need.
5. Always end with a final synthesis task that depends on every other task.
6. Use only the tickers given. Ids start at 1. Keep the plan between 5 and 9 tasks.

Respond with a JSON object matching the ResearchPlan schema."""


async def create_research_plan(question: str, tickers: list[str], feedback: str = "") -> ResearchPlan:
    user = f"QUESTION:\n{question}\n\nTICKERS: {', '.join(tickers)}"
    if feedback:
        user += f"\n\nThe previous attempt had these problems; plan to fix them:\n{feedback}"
    plan = await llm.parse(
        [{"role": "system", "content": PLANNER_PROMPT}, {"role": "user", "content": user}],
        ResearchPlan,
    )
    return normalize_plan(plan, tickers)


def normalize_plan(plan: ResearchPlan, tickers: list[str]) -> ResearchPlan:
    """Deterministic clean-up so the researcher can trust the plan."""
    allowed = {t.upper() for t in tickers}
    tasks: list[SubTask] = []
    for t in plan.sub_tasks:
        t.data_sources = [s for s in t.data_sources if s in SOURCES][:1]  # one tool per task
        t.tickers = [x.upper() for x in t.tickers if x.upper() in allowed] or list(allowed)
        if t.data_sources:
            t.dependencies = []          # data tasks never wait
        tasks.append(t)
    ids = {t.id for t in tasks}
    for t in tasks:                      # drop dependencies on tasks that don't exist (or itself)
        t.dependencies = [d for d in t.dependencies if d in ids and d != t.id]

    # Every company needs its own metrics task.
    have = {tk for t in tasks if t.data_sources == ["financial_api"] for tk in t.tickers}
    next_id = max(ids, default=0) + 1
    for tk in sorted(allowed - have):
        tasks.append(SubTask(id=next_id, description=f"Fetch {tk} revenue, growth, margins and P/E",
                             data_sources=["financial_api"], tickers=[tk]))
        next_id += 1
    # A financial_api task must be about exactly one company.
    split: list[SubTask] = []
    for t in tasks:
        if t.data_sources == ["financial_api"] and len(t.tickers) > 1:
            for tk in t.tickers:
                split.append(SubTask(id=next_id, description=f"{t.description} ({tk})",
                                     data_sources=["financial_api"], tickers=[tk]))
                next_id += 1
            for o in tasks:              # whoever waited for the multi-company task waits for all parts
                if t.id in o.dependencies:
                    o.dependencies = [d for d in o.dependencies if d != t.id] + [s.id for s in split[-len(t.tickers):]]
        else:
            split.append(t)
    tasks = split

    # The last task must synthesise everything.
    others = [t.id for t in tasks[:-1]]
    final = tasks[-1]
    if final.data_sources or set(final.dependencies) != set(others):
        if final.data_sources:
            tasks.append(SubTask(id=next_id, description="Synthesize all findings into the memo draft",
                                 data_sources=[], tickers=list(allowed)))
            final = tasks[-1]
        final.dependencies = [t.id for t in tasks if t.id != final.id]
    plan.sub_tasks = tasks
    return plan
