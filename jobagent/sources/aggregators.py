"""Public job-board APIs. All of these publish an open JSON feed for this use."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from ..models import Job
from .base import Source, infer_country, is_remote, register, strip_html


@register
class AdzunaSource(Source):
    """Adzuna's official search API - the only source with real IN/GB/US coverage.

    Free key: https://developer.adzuna.com/ (set ADZUNA_APP_ID / ADZUNA_APP_KEY).
    """

    name = "adzuna"
    requires_credentials = True
    BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
    COUNTRY_MAP = {"IN": "in", "GB": "gb", "US": "us"}

    def fetch(self) -> Iterable[Job]:
        app_id = self.conf.get("app_id", "")
        app_key = self.conf.get("app_key", "")
        if not app_id or not app_key:
            self._warn_missing()
            return
        pages = int(self.conf.get("pages", 2))
        results_per_page = int(self.conf.get("results_per_page", 50))

        for country in self.config.search.countries:
            code = self.COUNTRY_MAP.get(country)
            if not code:
                continue
            for title in self.config.search.titles:
                for page in range(1, pages + 1):
                    params = {
                        "app_id": app_id,
                        "app_key": app_key,
                        "results_per_page": results_per_page,
                        "what": title,
                        "max_days_old": self.config.search.max_age_days,
                        "content-type": "application/json",
                    }
                    min_salary = self.config.search.min_salary.get(country)
                    if min_salary:
                        params["salary_min"] = int(min_salary)
                    data = self.http.get_json(
                        self.BASE.format(country=code, page=page), params=params
                    )
                    results = (data or {}).get("results") or []
                    for item in results:
                        job = self._to_job(item, country)
                        if job:
                            yield job
                    if len(results) < results_per_page:
                        break  # last page for this query

    def _warn_missing(self) -> None:
        import logging
        logging.getLogger(__name__).warning(
            "adzuna enabled but app_id/app_key missing - skipping. "
            "Get a free key at https://developer.adzuna.com/"
        )

    def _to_job(self, item: dict, country: str) -> Job | None:
        url = item.get("redirect_url") or ""
        if not url:
            return None
        location = (item.get("location") or {}).get("display_name", "")
        description = strip_html(item.get("description", ""))
        return Job(
            source=self.name,
            title=item.get("title", "").strip(),
            company=((item.get("company") or {}).get("display_name") or "").strip(),
            url=url,
            location=location,
            country=infer_country(location) or country,
            remote=is_remote(location, item.get("title", "")),
            description=description,
            posted_at=item.get("created"),
            salary_min=item.get("salary_min"),
            salary_max=item.get("salary_max"),
            salary_currency={"IN": "INR", "GB": "GBP", "US": "USD"}.get(country),
            apply_method="assisted",
            raw={"adzuna_id": item.get("id")},
        )


@register
class RemotiveSource(Source):
    """Remotive's open remote-jobs API. No key needed."""

    name = "remotive"
    BASE = "https://remotive.com/api/remote-jobs"

    def fetch(self) -> Iterable[Job]:
        if not self.config.search.include_remote:
            return
        for term in self.conf.get("search_terms") or self.config.search.titles:
            data = self.http.get_json(self.BASE, params={"search": term, "limit": 50})
            for item in (data or {}).get("jobs", []):
                location = item.get("candidate_required_location", "") or "Remote"
                yield Job(
                    source=self.name,
                    title=(item.get("title") or "").strip(),
                    company=(item.get("company_name") or "").strip(),
                    url=item.get("url", ""),
                    location=location,
                    country=infer_country(location),
                    remote=True,
                    description=strip_html(item.get("description", "")),
                    posted_at=item.get("publication_date"),
                    salary_currency=None,
                    apply_method="assisted",
                    raw={"category": item.get("category")},
                )


@register
class RemoteOkSource(Source):
    """RemoteOK's public JSON feed. First element is legal metadata, not a job."""

    name = "remoteok"
    BASE = "https://remoteok.com/api"

    def fetch(self) -> Iterable[Job]:
        if not self.config.search.include_remote:
            return
        data = self.http.get_json(self.BASE, headers={"Accept": "application/json"})
        if not isinstance(data, list):
            return
        for item in data:
            if not isinstance(item, dict) or not item.get("position"):
                continue  # skips the leading disclaimer object
            location = item.get("location") or "Remote"
            yield Job(
                source=self.name,
                title=item["position"].strip(),
                company=(item.get("company") or "").strip(),
                url=item.get("url") or item.get("apply_url") or "",
                location=location,
                country=infer_country(location),
                remote=True,
                description=strip_html(item.get("description", "")),
                posted_at=item.get("date"),
                salary_min=item.get("salary_min") or None,
                salary_max=item.get("salary_max") or None,
                salary_currency="USD",
                apply_method="assisted",
                raw={"tags": item.get("tags", [])},
            )


@register
class ArbeitnowSource(Source):
    """Arbeitnow's free board API - EU-heavy but carries UK and remote roles."""

    name = "arbeitnow"
    BASE = "https://www.arbeitnow.com/api/job-board-api"

    def fetch(self) -> Iterable[Job]:
        pages = int(self.conf.get("pages", 3))
        for page in range(1, pages + 1):
            data = self.http.get_json(self.BASE, params={"page": page})
            items = (data or {}).get("data") or []
            if not items:
                break
            for item in items:
                location = item.get("location", "")
                posted = item.get("created_at")
                if isinstance(posted, (int, float)):
                    posted = datetime.fromtimestamp(posted, tz=timezone.utc).isoformat()
                yield Job(
                    source=self.name,
                    title=(item.get("title") or "").strip(),
                    company=(item.get("company_name") or "").strip(),
                    url=item.get("url", ""),
                    location=location,
                    country=infer_country(location),
                    remote=bool(item.get("remote")),
                    description=strip_html(item.get("description", "")),
                    posted_at=posted,
                    apply_method="assisted",
                    raw={"tags": item.get("tags", [])},
                )


@register
class JobicySource(Source):
    """Jobicy's free remote-jobs feed."""

    name = "jobicy"
    BASE = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self) -> Iterable[Job]:
        if not self.config.search.include_remote:
            return
        params = {"count": 50, "industry": self.conf.get("industry", "devops")}
        data = self.http.get_json(self.BASE, params=params)
        if data is None:
            # Jobicy rejects unknown industry slugs with a 400; the unfiltered
            # feed still works, and the matcher filters by content anyway.
            data = self.http.get_json(self.BASE, params={"count": 50})
        for item in (data or {}).get("jobs", []):
            location = item.get("jobGeo", "") or "Remote"
            yield Job(
                source=self.name,
                title=(item.get("jobTitle") or "").strip(),
                company=(item.get("companyName") or "").strip(),
                url=item.get("url", ""),
                location=location,
                country=infer_country(location),
                remote=True,
                description=strip_html(item.get("jobExcerpt", "") or item.get("jobDescription", "")),
                posted_at=item.get("pubDate"),
                salary_min=item.get("annualSalaryMin") or None,
                salary_max=item.get("annualSalaryMax") or None,
                salary_currency=item.get("salaryCurrency"),
                apply_method="assisted",
                raw={},
            )
