"""Company ATS boards (Greenhouse / Lever / Ashby).

These matter more than the aggregators: they publish an official JSON feed of
every open role *and* an official application endpoint, so these are the jobs
the agent can genuinely apply to end to end without touching a browser.
"""
from __future__ import annotations

import logging
from typing import Iterable

from ..models import Job
from .base import Source, infer_country, is_remote, register, strip_html

log = logging.getLogger(__name__)


class _BoardSource(Source):
    """Shared logic for 'iterate a list of company board tokens' sources."""

    def companies(self) -> list[str]:
        return [c for c in (self.conf.get("companies") or []) if c]

    def fetch(self) -> Iterable[Job]:
        tokens = self.companies()
        if not tokens:
            log.info("%s enabled but no companies configured", self.name)
            return
        for token in tokens:
            try:
                yield from self.fetch_company(token)
            except Exception as exc:  # noqa: BLE001 - one bad board shouldn't stop the rest
                log.warning("%s board %s failed: %s", self.name, token, exc)

    def fetch_company(self, token: str) -> Iterable[Job]:
        raise NotImplementedError


@register
class GreenhouseSource(_BoardSource):
    name = "greenhouse"
    BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

    def fetch_company(self, token: str) -> Iterable[Job]:
        data = self.http.get_json(self.BASE.format(token=token), params={"content": "true"})
        if not data:
            return
        for item in data.get("jobs", []):
            location = (item.get("location") or {}).get("name", "")
            offices = " ".join(
                o.get("name", "") for o in (item.get("offices") or []) if isinstance(o, dict)
            )
            title = (item.get("title") or "").strip()
            yield Job(
                source=self.name,
                title=title,
                company=_pretty(token),
                url=item.get("absolute_url", ""),
                location=location or offices,
                country=infer_country(location, offices),
                remote=is_remote(location, offices, title),
                description=strip_html(item.get("content", "")),
                posted_at=item.get("updated_at") or item.get("first_published"),
                apply_method="greenhouse",
                apply_meta={"board_token": token, "job_id": str(item.get("id"))},
                raw={"departments": [d.get("name") for d in item.get("departments", [])]},
            )


@register
class LeverSource(_BoardSource):
    name = "lever"
    BASE = "https://api.lever.co/v0/postings/{token}"

    def fetch_company(self, token: str) -> Iterable[Job]:
        data = self.http.get_json(self.BASE.format(token=token), params={"mode": "json"})
        if not isinstance(data, list):
            return
        for item in data:
            categories = item.get("categories") or {}
            location = categories.get("location", "") or ""
            workplace = item.get("workplaceType", "") or ""
            title = (item.get("text") or "").strip()
            yield Job(
                source=self.name,
                title=title,
                company=_pretty(token),
                url=item.get("hostedUrl", "") or item.get("applyUrl", ""),
                location=location,
                country=infer_country(location, categories.get("allLocations", [""])[0]
                                      if categories.get("allLocations") else ""),
                remote=workplace.lower() == "remote" or is_remote(location, title),
                description=strip_html(
                    item.get("descriptionPlain") or item.get("description", "")
                ),
                posted_at=_ms_to_iso(item.get("createdAt")),
                apply_method="lever",
                apply_meta={"site": token, "posting_id": item.get("id", "")},
                raw={"team": categories.get("team"), "commitment": categories.get("commitment")},
            )


@register
class AshbySource(_BoardSource):
    name = "ashby"
    BASE = "https://api.ashbyhq.com/posting-api/job-board/{token}"

    def fetch_company(self, token: str) -> Iterable[Job]:
        data = self.http.get_json(
            self.BASE.format(token=token), params={"includeCompensation": "true"}
        )
        if not data:
            return
        for item in data.get("jobs", []):
            location = item.get("location", "") or ""
            title = (item.get("title") or "").strip()
            yield Job(
                source=self.name,
                title=title,
                company=item.get("companyName") or _pretty(token),
                url=item.get("jobUrl", "") or item.get("applyUrl", ""),
                location=location,
                country=infer_country(location),
                remote=bool(item.get("isRemote")) or is_remote(location, title),
                description=strip_html(item.get("descriptionHtml", "")
                                       or item.get("descriptionPlain", "")),
                posted_at=item.get("publishedAt"),
                # Ashby's public post endpoint needs a per-company API key, so we
                # hand these to the assisted flow instead of pretending we can post.
                apply_method="assisted",
                apply_meta={"board": token, "job_id": item.get("id", "")},
                raw={"department": item.get("department"), "team": item.get("team")},
            )


def _pretty(token: str) -> str:
    """'thought-machine' -> 'Thought Machine'. Board tokens are our only company name."""
    return token.replace("-", " ").replace("_", " ").title()


def _ms_to_iso(value) -> str | None:
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None
