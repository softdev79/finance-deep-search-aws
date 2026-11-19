# Finance Deep Search on AWS: an equity research agent with an eval release gate

**Author:** Vishal Gupta · built from Section 5 ("Deep Search"), Section 11 (evaluation) and Section 12 (production)
of the *AI Agents in Finance* course by Shivank Agrawal.

Ask a research question about 1-3 companies, for example *"Compare NVIDIA with AMD and Intel on revenue growth,
profitability and key risk factors"*. The agent plans the research, runs the data tasks in parallel waves,
checks the findings with plain code (never an LLM), re-plans if something is missing, and writes a structured
memo with a metrics table, risks and sources. Every step is visible live in the browser.

```
Browser ─▶ Lambda Function URL "api" (X-API-Key) ─▶ StartExecution ─┐        UI polls the run record every 2.5 s
                                                                    ▼
        AWS Step Functions (Standard)                                          
        Plan ─▶ Map over waves (1 at a time) ─▶ Map over tasks (3 parallel) ─▶ Validate ─▶ Choice
          ▲                                                                                 │
          └──────────── re-plan with the validator's gaps (max 2) ◀─────────────────────────┤
                                                                         Synthesize ◀───────┘   Catch → MarkFailed
        every state = the same "worker" Lambda (Python 3.13, arm64), routed by event["step"]
          ├─▶ Amazon Bedrock (OpenAI-compatible endpoint, short-term token signed by the Lambda role)
          │     gpt-oss-120b: plan, compare, memo · gpt-oss-20b: 10-K summaries (reasoning_effort=low for routine steps)
          ├─▶ SEC EDGAR: XBRL company facts (financials) + latest 10-K "Item 1A Risk Factors"
          ├─▶ Tavily: news from trusted publishers only
          ├─▶ DynamoDB: run record (plan, per-task status, attempts, tokens, $) + tool cache with TTL
          └─▶ S3: every plan, task result, validation and memo (replayable runs)
SSM Parameter Store (keys) · CloudWatch + X-Ray · alarm on failed executions · AWS Budgets
```

## What I added on top of the course code

| Area | Course (Section 5) | This project |
|---|---|---|
| Runtime | one Python process with asyncio | **Step Functions** states + Lambda workers; only IDs travel between states, state lives in DynamoDB/S3 |
| LLM | OpenAI `gpt-4o`, strict structured output | Bedrock **gpt-oss-120b/20b** with no stored key; tolerant JSON parsing (gpt-oss adds stray `**`/`##` before JSON); model tiering + `reasoning_effort` |
| Tickers | hard-coded NVDA/AMD/INTC name matching | planner fills `tickers` per task; `normalize_plan()` fixes bad plans in code (one tool per task, one company per metrics task, synthesis last) |
| SEC tool | used but not shown | written from scratch: EDGAR 10-K lookup, Item 1A extraction (with a fallback for Intel's non-standard layout), 10-K "intern" summary |
| Metrics | Financial Datasets API | same, with automatic fallback to **SEC XBRL company facts** (free; merges tag changes such as Apple's 2018 revenue tag switch) |
| Validator | 3 checks | + failed-task reasons fed to the re-plan, + partial-data warnings |
| Reliability | none | retries with 429 backoff, cache with TTL, per-run token budget, Step Functions retry/catch, run timeout |
| Quality proof | none | **eval harness + release gate** (below) |

## Evaluation and release gate (Section 11 pattern)

`evals/` runs 6 research questions (AI chips, Apple vs Microsoft, Coca-Cola vs PepsiCo, JPMorgan vs Bank of
America, Tesla vs Ford, Amazon vs Walmart) through the same step handlers the cloud uses, then scores the saved runs.
Most checks are code against the run's own tool outputs:

| Metric | What it checks | Threshold | Result |
|---|---|---|---|
| run_ok | every run finished | 1.0 (BLOCK) | **1.000** |
| completeness | every company has a metrics row and a risk bullet; all sections present | 0.95 (BLOCK) | **1.000** |
| factual_accuracy | every number in the metrics table matches the tool output within 0.5% | 1.0 (BLOCK) | **1.000** |
| no_invented_values | P/E and market cap say n/a when the data has none | 1.0 (BLOCK) | **1.000** |
| citations_valid | every cited URL actually appeared in the findings | 1.0 (BLOCK) | **1.000** |
| tenk_cited | every 10-K used is cited | 0.9 | **1.000** |
| number_grounding | % figures in the prose exist in the findings | 0.85 | **0.942** |
| avg_cost_usd | per run | ≤ $0.05 | **$0.010** |
| p95_seconds | per run | ≤ 300 | **130** |
| judge_avg | LLM judge, structure + reasoning (1-5, strict rubric) | 3.5 | **3.0 → CONDITIONAL** |

Verdict: **CONDITIONAL** (exit code 1): every hard check passes; the strict LLM judge scores the reasoning at 3.0/5,
so the memos' argumentation is the next thing to improve. The gate is proven by fault injection; each of
`--fault drop_ticker | wrong_units | fake_source | invent_pe` returns **BLOCK** (exit 2).

The harness also caught four real bugs during development, all fixed: a race in the local run store, Bedrock
tokens outliving the `aws login` session, a token budget accidentally shared between runs (it would also have hit
warm Lambdas), and a 10-K parser that swallowed the table of contents.

## Live deployment (us-east-2)

* Smoke test through the public URL: page 200, health 200, wrong key 403, full research run **done in 73 s,
  27k tokens, $0.0087**, validator passed on the first attempt (`docs/evidence/smoke-test-live.txt`).
* One run is ~17 states / ~30-35 billable transitions → about 120 runs a month inside the Step Functions free tier.
* Sample memo: `docs/evidence/sample-memo-ai-chips.md`. Every number in its table matches SEC EDGAR exactly.

## Run it

```powershell
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
copy .env.example .env      # TAVILY_API_KEY, SEC_USER_AGENT ("Name app you@example.com"), API_KEY; FINANCIAL_DATASETS_API_KEY optional
aws login                   # Bedrock tokens are signed from your AWS session

.venv\Scripts\python -m pytest -q                       # 10 offline tests
.venv\Scripts\python deep_search_agent.py               # one memo, locally (asyncio)
.venv\Scripts\python -m scripts.simulate_workflow       # same, through the Step Functions step handlers
.venv\Scripts\python -m evals.run_cases                 # 6 eval runs (~6 min, ~$0.06)
.venv\Scripts\python -m evals.score --judge             # scorecard + gate (exit 0/1/2)
```

## Deploy / tear down

```powershell
.\scripts\deploy.ps1            # secrets → SSM, zip build, SAM deploy, live smoke test
.\scripts\deploy.ps1 -Destroy   # delete stack, run artefacts and build bucket
```

## Cost

| Item | Per memo |
|---|---|
| Bedrock (gpt-oss, ~27k tokens) | ~$0.01 |
| Step Functions (~35 transitions) | $0 within 4,000/month |
| Lambda, DynamoDB, S3, SSM, CloudWatch, X-Ray | $0 at demo volume |
| SEC EDGAR | free · Tavily free plan: 1,000 searches/month |
| Idle | **$0** |

Memos are AI-generated research summaries for education, not investment advice (the disclaimer is added in code).
