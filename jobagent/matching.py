"""Scoring a job against the profile.

Two stages, cheap then expensive:

1. `score_job` - deterministic. Hard blockers first (wrong country, excluded
   company, stale posting, junior role), then a weighted 0-100 score from skill
   overlap, title fit, seniority and salary. Free, runs on every job.
2. `llm_rescore` - the LLM reads the top N descriptions and gives a considered
   verdict. Catches what keywords miss ("Kubernetes" in a job that is really a
   Java backend role) in both directions.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import Config
from .models import Job, MatchResult, Profile

log = logging.getLogger(__name__)

# Weights sum to 100.
W_SKILLS = 45
W_TITLE = 25
W_SENIORITY = 15
W_KEYWORDS = 10
W_SALARY = 5

# Titles that are a hard no for a lead-level candidate.
JUNIOR_MARKERS = re.compile(
    r"\b(intern|internship|graduate|trainee|apprentice|junior|jr\.?|entry[\s-]level|"
    r"fresher|associate\s+(?:i|1)\b|student)\b", re.I)

SENIORITY_MARKERS = {
    "head": 1.0, "director": 1.0, "vp": 1.0, "principal": 1.0, "staff": 0.95,
    "lead": 1.0, "manager": 0.9, "senior": 0.8, "sr": 0.8, "iii": 0.7, "iv": 0.8,
}

# Skills that carry more signal for a DevOps lead than a generic keyword hit.
HIGH_VALUE_SKILLS = {
    "kubernetes", "terraform", "aws", "gcp", "azure", "argocd", "gitops",
    "sre", "platform engineering", "observability", "ci/cd", "docker",
    "prometheus", "helm", "python", "go", "golang", "vault", "istio",
}


def parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _skill_overlap(profile: Profile, job: Job) -> tuple[float, list[str]]:
    """Fraction of the candidate's skills the posting mentions, weighted."""
    text = job.text.lower()
    if not profile.skills:
        return 0.0, []
    hits, weight, total = [], 0.0, 0.0
    for skill in profile.skills:
        value = 2.0 if skill in HIGH_VALUE_SKILLS else 1.0
        total += value
        if re.search(rf"(?<![\w+#]){re.escape(skill)}(?![\w+#])", text):
            hits.append(skill)
            weight += value
    if not total:
        return 0.0, []
    # Normalise against a realistic ceiling: a posting naming ~40% of your stack
    # is already an excellent match, so treat that as full marks.
    return min(1.0, (weight / total) / 0.40), hits


def _title_fit(config: Config, job: Job) -> tuple[float, str]:
    title = job.title.lower()
    best, label = 0.0, ""
    for wanted in config.search.titles:
        want = wanted.lower()
        if want == title:
            return 1.0, wanted
        want_words = set(re.findall(r"[a-z]+", want))
        title_words = set(re.findall(r"[a-z]+", title))
        if not want_words:
            continue
        overlap = len(want_words & title_words) / len(want_words)
        if overlap > best:
            best, label = overlap, wanted
    return best, label


def _seniority_fit(config: Config, job: Job) -> tuple[float, str]:
    title = job.title.lower()
    for marker, value in SENIORITY_MARKERS.items():
        if re.search(rf"\b{re.escape(marker)}\b", title):
            return value, marker
    return 0.45, ""  # unmarked title: plausible, not a strong signal


def _salary_fit(config: Config, job: Job) -> float:
    floor = config.search.min_salary.get(job.country)
    if not floor or job.salary_max is None:
        return 0.5  # unknown salary is neutral, never a penalty
    top = job.salary_max or job.salary_min or 0
    if top >= floor * 1.25:
        return 1.0
    return 1.0 if top >= floor else 0.0


def score_job(config: Config, profile: Profile, job: Job) -> MatchResult:
    """Deterministic 0-100 score plus any hard blockers."""
    result = MatchResult(score=0.0)
    search = config.search

    # --- hard blockers ---------------------------------------------------------
    if JUNIOR_MARKERS.search(job.title):
        result.blockers.append(f"junior-level title: {job.title!r}")

    for pattern in search.exclude_titles:
        if re.search(re.escape(pattern), job.title, re.I):
            result.blockers.append(f"excluded title pattern {pattern!r}")

    for company in search.exclude_companies:
        if company.strip().lower() == job.company.strip().lower():
            result.blockers.append(f"excluded company {job.company!r}")

    if job.country and job.country not in search.countries:
        result.blockers.append(f"country {job.country} not in {search.countries}")
    elif not job.country and not (job.remote and search.include_remote):
        result.blockers.append("location could not be resolved to a target country")

    if search.remote_only and not job.remote:
        result.blockers.append("remote_only is set and this role is not remote")
    if not search.include_remote and job.remote:
        result.blockers.append("remote roles excluded by config")

    posted = parse_date(job.posted_at)
    if posted and posted < datetime.now(timezone.utc) - timedelta(days=search.max_age_days):
        result.blockers.append(f"posted {posted.date()}, older than {search.max_age_days}d")

    if result.blockers:
        return result

    # --- weighted score --------------------------------------------------------
    skill_score, hits = _skill_overlap(profile, job)
    title_score, matched_title = _title_fit(config, job)
    seniority_score, marker = _seniority_fit(config, job)
    salary_score = _salary_fit(config, job)

    text = job.text.lower()
    keywords = [k for k in search.keywords if k.lower() in text]
    keyword_score = min(1.0, len(keywords) / max(1, min(4, len(search.keywords) or 1)))

    result.score = round(
        skill_score * W_SKILLS
        + title_score * W_TITLE
        + seniority_score * W_SENIORITY
        + keyword_score * W_KEYWORDS
        + salary_score * W_SALARY,
        1,
    )

    if hits:
        result.reasons.append(
            f"{len(hits)} matching skills: {', '.join(sorted(hits)[:8])}"
            + (" ..." if len(hits) > 8 else "")
        )
    if matched_title and title_score >= 0.5:
        result.reasons.append(f"title aligns with {matched_title!r} ({title_score:.0%})")
    if marker:
        result.reasons.append(f"seniority signal: {marker}")
    if keywords:
        result.reasons.append(f"keywords: {', '.join(keywords[:5])}")
    if job.remote:
        result.reasons.append("remote")
    if job.salary_max:
        currency = job.salary_currency or ""
        result.reasons.append(f"salary up to {currency} {job.salary_max:,.0f}".strip())
    return result


def rank(config: Config, profile: Profile, jobs: list[Job]) -> list[tuple[Job, MatchResult]]:
    """Score every job, drop the blocked ones, best first."""
    scored = []
    for job in jobs:
        result = score_job(config, profile, job)
        if result.passed:
            scored.append((job, result))
        else:
            log.debug("blocked %s @ %s: %s", job.title, job.company, result.blockers)
    scored.sort(key=lambda pair: pair[1].score, reverse=True)
    return scored


LLM_MATCH_PROMPT = """You are screening a job for a candidate. Be honest and strict \
- a bad application wastes the candidate's credibility.

CANDIDATE
Name: {name}
Headline: {headline}
Years of experience: {years}
Skills: {skills}
Recent titles: {titles}
Summary: {summary}

JOB
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Return ONLY JSON:
{{"score": <0-100 fit>, "recommend": <true|false>, "rationale": "<one sentence>", \
"concerns": ["<gap or mismatch>", ...]}}

Score on: does the candidate clear the bar, is the seniority right, is the core \
tech stack a genuine overlap. Penalise heavily if the role is a different \
discipline that merely mentions the same tools, or if it is materially more \
junior than the candidate."""


def llm_rescore(config: Config, profile: Profile, job: Job, result: MatchResult, llm) -> MatchResult:
    """Ask the model for a considered verdict; blend it with the keyword score."""
    from .resume import _json_from

    raw = llm.complete(
        LLM_MATCH_PROMPT.format(
            name=profile.full_name or "the candidate",
            headline=profile.headline or "-",
            years=profile.years_experience or "unknown",
            skills=", ".join(profile.skills[:40]) or "-",
            titles=", ".join(profile.titles[:6]) or "-",
            summary=profile.summary or "-",
            title=job.title, company=job.company,
            location=job.location or ("Remote" if job.remote else "-"),
            description=(job.description or "")[:6000],
        ),
        max_tokens=600,
        system="You are a blunt technical recruiter. Return strict JSON only.",
    )
    data = _json_from(raw)
    if not data:
        return result

    try:
        result.llm_score = float(data.get("score", 0))
    except (TypeError, ValueError):
        return result
    result.llm_rationale = str(data.get("rationale", "")).strip()

    # The LLM read the description; weight it accordingly, but keep the
    # keyword score in the blend so one odd verdict can't dominate.
    result.score = round(result.score * 0.35 + result.llm_score * 0.65, 1)
    if result.llm_rationale:
        result.reasons.append(f"AI: {result.llm_rationale}")
    for concern in (data.get("concerns") or [])[:3]:
        result.reasons.append(f"concern: {concern}")
    if data.get("recommend") is False:
        result.blockers.append(f"AI screen says do not apply: {result.llm_rationale}")
    return result
