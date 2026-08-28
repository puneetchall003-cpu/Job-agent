"""Core data structures shared across sources, matching, and apply drivers."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

# Application lifecycle. A job moves left to right; `skipped` and `failed` are terminal.
STATUS_NEW = "new"              # discovered, not yet scored
STATUS_MATCHED = "matched"      # scored above threshold, awaiting tailoring
STATUS_TAILORED = "tailored"    # resume + cover letter generated
STATUS_QUEUED = "queued"        # ready to submit (auto) or ready for you to review (assisted)
STATUS_APPLIED = "applied"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = {STATUS_APPLIED, STATUS_SKIPPED}


def _norm(text: str) -> str:
    """Lowercase and collapse everything that isn't a letter or digit."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@dataclass
class Job:
    """A single job posting, normalised across every source."""

    source: str                       # e.g. "greenhouse", "adzuna", "linkedin"
    title: str
    company: str
    url: str
    location: str = ""
    country: str = ""                 # ISO-ish code we bucket on: IN / GB / US
    remote: bool = False
    description: str = ""
    posted_at: Optional[str] = None   # ISO 8601
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    # How this job can be applied to: "greenhouse", "lever", "assisted", "manual".
    apply_method: str = "manual"
    # Source-specific handles the apply drivers need (board token, posting id, ...).
    apply_meta: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Stable id used for dedupe across sources.

        Deliberately excludes the URL: the same role is routinely listed on
        LinkedIn, the company board, and an aggregator with three different
        links, and we only want to apply once.
        """
        basis = f"{_norm(self.company)}|{_norm(self.title)}|{_norm(self.location)}"
        return hashlib.sha256(basis.encode()).hexdigest()[:20]

    @property
    def text(self) -> str:
        """Everything we match against, as one blob."""
        return f"{self.title}\n{self.company}\n{self.location}\n{self.description}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        return d


@dataclass
class Profile:
    """You: parsed from the resume, then overlaid with config answers."""

    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    headline: str = ""
    summary: str = ""
    years_experience: Optional[float] = None
    skills: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)      # past job titles
    companies: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    resume_path: str = ""
    resume_text: str = ""
    # Free-form answers for application forms, keyed by question intent
    # (notice_period, visa_uk, salary_expectation_usd, ...). See config.yaml.
    answers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MatchResult:
    score: float                       # 0-100
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    llm_score: Optional[float] = None
    llm_rationale: str = ""

    @property
    def passed(self) -> bool:
        return not self.blockers


@dataclass
class Application:
    """A job we have decided to act on, plus everything generated for it."""

    fingerprint: str
    job: Job
    status: str = STATUS_NEW
    score: float = 0.0
    match_reasons: list[str] = field(default_factory=list)
    cover_letter: str = ""
    tailored_resume: str = ""
    resume_file: str = ""             # path to the PDF/DOCX actually submitted
    answers: dict[str, str] = field(default_factory=dict)
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    applied_at: Optional[str] = None

    def touch(self, status: Optional[str] = None) -> None:
        if status:
            self.status = status
        self.updated_at = datetime.now(timezone.utc).isoformat()
        if status == STATUS_APPLIED and not self.applied_at:
            self.applied_at = self.updated_at
