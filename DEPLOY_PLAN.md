# Finance Deep Search agent on AWS: deployment plan (free-tier first)

Source: `week3/homework/finance` (course "AI Agents in Finance"). Core = **Section 5, Deep Search** (planner →
parallel research waves → plain-code validator → synthesizer, with re-plan). Added on top: **Section 11's release
gate** (evals block bad releases) and **Section 12's production patterns** (budgets, durable runs, storage, tiering).
The course code lives in `<pre>` blocks inside the HTML; step 1 extracts it into real files.

Target: a web page where you type a research question and tickers (e.g. *"Compare NVIDIA with AMD and Intel on
growth, profitability and risks"*), watch the plan and each task run, and get a structured investment memo with
sources. **$0 when idle**, cents per memo.

## 1. What the course agent does (unchanged logic)

```
question + tickers
   │
Planner (LLM, structured output) ── ResearchPlan: N SubTasks with dependencies
   │      e.g. 1-3 metrics per company · 4 risk factors (10-K) · 5 market news · 6 compare (needs 1-3) · 7 draft (needs 4-6)
Researcher ── runs tasks in waves: everything whose dependencies are done runs at the same time
   │      tools (no LLM): get_financial_metrics (Financial Datasets API) · get_risk_factors (SEC EDGAR 10-K Item 1A)
   │                      · search_financial_news (Tavily); "thinking" tasks 6-7 use the LLM
Validator (plain Python, never an LLM) ── every task done? every ticker covered? numbers possible and plausible?
   │      gaps or errors → re-plan (max 2 retries)
Synthesizer (LLM, structured output) ── memo: summary, per-company metrics table, comparison, risks, sources
```

## 2. AWS architecture

```
Browser ──HTTPS──▶ Lambda Function URL  "api"  (public; /api/* needs X-API-Key)      UI: static page served by the same Lambda
                     POST /api/runs        → StartExecution, returns run_id
                     GET  /api/runs/{id}   → status, plan, task states, memo  (UI polls every 2 s)
                     GET  /api/runs        → history
                          │
                AWS Step Functions (Standard)  "deep-search"
   ┌───────────────────────────────────────────────────────────────────────────────────────────┐
   │ Plan ─▶ Map over waves (1 at a time) ─▶ inner Map over tasks (MaxConcurrency 3) ─▶ Validate │
   │   ▲                                                                                │       │
   │   └──────────── Choice: gaps and attempt < 3 → Plan again (with the validator's gaps) ◀────┘       │
   │                                             └─ ok → Synthesize ─▶ Done                     │
   │ Catch on every step → MarkFailed;  run-level timeout 15 min                                 │
   └───────────────────────────────────────────────────────────────────────────────────────────┘
        each step = the same "worker" Lambda, routed by event["step"]
          ├──▶ Bedrock (OpenAI-compatible endpoint, role-signed short-term token)
          │       planner + synthesizer + compare/draft: openai.gpt-oss-120b
          │       10-K summariser + light extraction:   openai.gpt-oss-20b   (Section 12 "small model for small jobs")
          ├──▶ Financial Datasets API · SEC EDGAR · Tavily   (keys in SSM; httpx with retries + 429 backoff)
          ├──▶ DynamoDB  runs   (run_id → status, plan, task states, timings, cost)         TTL 90 days
          ├──▶ DynamoDB  cache  (tool#args → result)  metrics 7 d · 10-K 30 d · news 6 h   TTL
          └──▶ S3        runs/<run_id>/plan.json, task-<n>.json, validation.json, memo.md  (Section 12: "store everything")
Ops: CloudWatch Logs (7 d) + metrics (tokens, $ per run, validator failures) + alarm · X-Ray on Lambda and Step Functions
     AWS Budgets $5 · SSM Parameter Store (API keys, SEC User-Agent, X-API-Key)
CI:  GitHub Actions → unit tests + eval harness (Section 11 style) → exit code 0 SHIP / 1 CONDITIONAL / 2 BLOCK → deploy only on 0
```

Why Step Functions and not one long Lambda: runs take 30 s to several minutes, the waves are naturally parallel,
every step is retried and visible in the console, a crash resumes from the failed step (Section 12's "survive
crashes"), and Standard workflows have an **always-free 4,000 state transitions a month**.

## 3. Services and free-tier fit

| Need | Service | Free tier | Expected use |
|---|---|---|---|
| UI + API | Lambda + Function URL | 1M requests, 400k GB-s / month, always | a few hundred calls |
| Orchestration | Step Functions Standard | 4,000 transitions / month, always | ~30-40 per run → ~100 runs / month |
| Workers | Lambda (zip, arm64, 1024 MB) | same as above | ~10-15 invocations per run |
| Run state, tool cache | DynamoDB on-demand | 25 GB, always | KB-scale |
| Run artefacts | S3 | 5 GB (12-month) or credits | KB-scale; lifecycle 90 days |
| Secrets | SSM Parameter Store standard | free | 5 parameters |
| Logs, metrics, alarms, traces | CloudWatch + X-Ray | 5 GB logs, 10 alarms, 100k traces | small |
| Cost guard | AWS Budgets | 2 budgets free | 1 |
| Eval gate | GitHub Actions (public repo) | free | per push |
| LLM | Bedrock (gpt-oss-120b / 20b) | **not free tier** | ~$0.01-0.03 per memo |
| Data | Financial Datasets API, Tavily, SEC EDGAR | their own free plans; EDGAR free | see risks |

No container image needed: dependencies are small (openai, pydantic, httpx, boto3 in runtime), so the zip build
from `inventory_agent_serverless_aws` is reused.

## 4. Build steps

**0. Prerequisites (you)**
- `aws login`
- Financial Datasets API key (financialdatasets.ai; check the current free plan limits first)
- Tavily API key (free plan: 1,000 searches a month)
- An email for the SEC EDGAR `User-Agent` header (SEC requires a contact; it is stored only in SSM, never in code)
- Optional but helpful: AWS Support case to lift the CloudFront and classic Bedrock runtime restrictions

**1. Extract and run locally (no AWS).** Pull Section 5's code blocks into `common.py`, `planner.py`,
`tools/{financial_api,sec_filings,web_search}.py`, `researcher.py`, `validator.py`, `synthesizer.py`,
`deep_search_agent.py`. Point the OpenAI client at Bedrock (role-signed token, `settings.py` from the AI Tutor).
Run the course question end to end; save the memo as the baseline.

**2. Split into step handlers.** `steps.py` with pure functions `plan(run)`, `run_task(run, task)`,
`validate(run)`, `synthesize(run)`. Each reads and writes state in DynamoDB/S3 so Step Functions passes only IDs
(payload limit 256 KB). Keep `deep_search_agent.py` as a local runner that calls the same functions with asyncio,
so local and cloud stay identical. Add `RunBudget` from Section 12 (max steps, tokens, $ per run) and record
token use per step.

**3. Tools hardened.** httpx with timeouts, retries with backoff on 429/5xx, a DynamoDB cache with TTL, and the
10-K "intern" from Section 12: summarise Item 1A (often 20k+ tokens) to ~500 tokens with gpt-oss-20b before it
reaches the planner or synthesizer.

**4. Infrastructure (`template.yaml`, SAM).** Two Lambdas (api, worker), the state machine (ASL in the template),
2 DynamoDB tables, S3 bucket with lifecycle, IAM least privilege (worker: Bedrock token actions, the two tables,
`runs/*` in the bucket, the SSM parameters; api: `states:StartExecution` + read access), log groups, alarm on
failed executions, budget. Reuse the zip build from the inventory deploy script.

**5. UI.** One HTML page: question + tickers form, live plan graph (tasks coloured by status from the polled run
record), validator verdict, the memo rendered as Markdown with a sources list, and a history of past runs.

**6. Eval harness + release gate (Section 11 pattern).**
- `evals/cases.jsonl`: ~15 research questions over ~10 large-cap tickers.
- Checks in code: completeness (every ticker, every memo section), **factual accuracy** (metrics in the memo
  match the Financial Datasets numbers within 0.5%), consistency (same number quoted the same everywhere),
  citations present and resolving.
- LLM judge (gpt-oss-120b with a rubric) for structure and reasoning, reported with its own agreement check.
- Scorecard with thresholds → `SHIP` (exit 0) / `CONDITIONAL` (1) / `BLOCK` (2) + `report.md`.
- A fault switch (`--fault drop_ticker`, `--fault wrong_units`) proves the gate actually blocks.
- GitHub Actions: tests + offline eval on every push (cached tool data, no live APIs); live eval on demand.

**7. Deploy, smoke test, evidence.** Deploy, run the course question live, capture the Step Functions graph
screenshot, the memo, per-run cost and timings, then README, public repo and LinkedIn text.

**Phase 2 (optional): overnight batch from Section 12.** EventBridge Scheduler (6 am) → SQS → worker Lambda with
reserved concurrency 2 → one-paragraph note per ticker into S3 + a digest page. Runs outside Step Functions to stay
within its free tier. Start with 20 tickers, not 500 (Bedrock cost and API limits).

## 5. Cost

| Item | Per memo | 100 memos / month |
|---|---|---|
| Bedrock: ~25-30k tokens, mostly gpt-oss-120b, summaries on 20b | ~$0.01-0.03 | ~$1-3 |
| Step Functions (~35 transitions) | $0 within 4,000 / month | $0 |
| Lambda, DynamoDB, SSM, CloudWatch | $0 | $0 |
| S3 artefacts | ~$0 | cents |
| Idle | | **$0** |

The course estimates ~$0.15 per run at GPT-4o-class rates; gpt-oss on Bedrock is roughly 5-10× cheaper.

## 6. Risks

| Risk | Mitigation |
|---|---|
| yfinance blocked from AWS IPs (used heavily in the course) | Not used in the cloud path; Section 5 already uses Financial Datasets + SEC EDGAR |
| Financial Datasets free plan too small | Check limits at step 0; cache filed numbers for 7 days; fall back to SEC EDGAR XBRL "company facts" (free) for revenue/net income |
| Lambda account concurrency limit (10) | Inner Map MaxConcurrency 3; batch worker reserved concurrency 2 |
| Step Functions free tier exhausted | ~100 runs/month fits; batch path bypasses Step Functions; Budgets alert |
| gpt-oss structured output differs from GPT-4o | Pydantic validation + one repair retry; smoke test in step 1 |
| 10-K text too long | Summariser "intern" step; token budget per task |
| Public URL abuse | X-API-Key, run budget ($ cap per run), max 3 tickers per run, Budgets alert |
| Investment-advice framing | Memo footer: "research summary for education, not investment advice" |
| Course code licensing | Credit Shivank Agrawal's course in README; ask before publishing large verbatim parts |

## 7. Order of work

1. Keys + local extraction + Bedrock smoke test (proves models, data APIs and structured output).
2. Step handlers + local runner + tool hardening + cache.
3. Eval harness and gate locally (baseline scorecard).
4. SAM template + deploy script, deploy, first live run.
5. UI, evidence, README, public repo.
6. Phase 2 batch if wanted.
