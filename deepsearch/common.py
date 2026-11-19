"""common.py - the "forms" (Pydantic models) every part of the agent fills in.

From "AI Agents in Finance", Section 5 (Shivank Agrawal). AWS edition changes: SubTask gains
`tickers` (so tools work for any company, not a hard-coded list) and `error`.
"""
from enum import Enum
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):         # the 4 stickers a task can wear
    PENDING = "pending"              # not started yet
    RUNNING = "running"              # a tool is working on it
    COMPLETED = "completed"          # finished, result saved
    FAILED = "failed"                # the tool raised an error


class SubTask(BaseModel):
    """A single research sub-task within a plan."""
    id: int                                          # 1, 2, 3 …
    description: str                                 # the job, in plain English
    data_sources: list[str] = Field(
        description="Tools: 'financial_api', 'sec_filing', 'web_search'"
    )
    tickers: list[str] = Field(default_factory=list, description="Tickers this task is about")
    dependencies: list[int] = Field(default_factory=list)  # ids to wait for
    status: TaskStatus = TaskStatus.PENDING          # only OUR code changes this
    error: str | None = None                         # set by our code on failure


class ResearchPlan(BaseModel):                       # what the planner returns
    question: str
    sub_tasks: list[SubTask]


class CompanyMetrics(BaseModel):                     # one company, clean numbers
    ticker: str
    revenue: float | None = None             # $ millions
    revenue_growth: float | None = None      # % vs the year before
    eps: float | None = None                 # earnings per share, $
    gross_margin: float | None = None        # %
    operating_margin: float | None = None    # %
    pe_ratio: float | None = None            # price ÷ EPS
    market_cap: float | None = None          # $
    source: str | None = None                # which data provider (AWS edition)


class ValidationResult(BaseModel):                   # the checker's report card
    is_valid: bool
    errors: list[str] = Field(default_factory=list)     # impossible numbers
    warnings: list[str] = Field(default_factory=list)   # odd but possible
    gaps: list[str] = Field(default_factory=list)       # missing pieces


class ResearchReport(BaseModel):                     # the final memo
    title: str
    executive_summary: str
    key_metrics_table: str
    competitive_analysis: str
    risk_factors: list[str]
    conclusion: str
    sources: list[str]
    confidence_score: float = Field(description="Between 0 and 1")