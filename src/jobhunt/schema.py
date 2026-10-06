"""Data models shared across the pipeline.

Everything that crosses a module boundary is a Pydantic model so that
shape errors surface at the edge, not three functions later.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ATSName = Literal[
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable", "bamboohr",
    "amazon", "eightfold",
]

_WORKDAY_DATACENTER = re.compile(r"wd\d+")
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", re.I)


class Company(BaseModel):
    """One entry from config/companies.yaml."""

    name: str
    ats: ATSName
    slug: str  # Workday: "tenant/site", e.g. "adobe/external_experienced"
    datacenter: str | None = None  # Workday only: the "wd5" in adobe.wd5.myworkdayjobs.com
    location: str | None = None  # Eightfold only: limit searches to a place, e.g. "United States"
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _workday_board(self) -> Company:
        if self.ats == "workday":
            tenant, _, site = self.slug.partition("/")
            if not tenant or not site or "/" in site:
                raise ValueError("a workday slug is tenant/site, e.g. adobe/external_experienced")
            if not self.datacenter or not _WORKDAY_DATACENTER.fullmatch(self.datacenter):
                raise ValueError("a workday board needs datacenter: wdN, e.g. wd5")
        if self.ats == "eightfold" and not _HOST.fullmatch(self.slug):
            raise ValueError("an eightfold slug is a careers site host, e.g. eaton.eightfold.ai")
        if self.location is not None and self.ats != "eightfold":
            raise ValueError("location: is only for eightfold boards")
        return self

    @property
    def key(self) -> str:
        """Identifies one board. Matches the ``source:company_slug`` prefix of its jobs' keys."""
        return f"{self.ats}:{self.slug}"


class Job(BaseModel):
    """A normalized job posting, regardless of which ATS it came from."""

    source: ATSName
    company: str
    company_slug: str
    external_id: str
    title: str
    location: str = ""
    remote: bool | None = None  # None = unknown
    url: str
    body: str = ""  # plain text; HTML stripped
    posted_at: str | None = None  # ISO 8601, UTC
    fetched_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def key(self) -> str:
        """Dedupe key. Stable across runs."""
        return f"{self.source}:{self.company_slug}:{self.external_id}"

    @field_validator("title", "location", "body", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class Score(BaseModel):
    """Claude's verdict on one job."""

    score: int = Field(ge=1, le=10)
    rationale: str
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    suggested_modules: list[str] = Field(default_factory=list, max_length=2)
    model: str


class ScoredJob(BaseModel):
    job: Job
    score: Score


class Letter(BaseModel):
    job_key: str
    company: str
    title: str
    modules_used: list[str]
    text: str
    model: str
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
