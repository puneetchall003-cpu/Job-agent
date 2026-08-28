"""LinkedIn support.

Read this before enabling anything here.

LinkedIn's User Agreement forbids scraping and automated activity, and they
enforce it with account restrictions. Your LinkedIn account is the single most
valuable asset in your job search, so this module defaults to *not* automating
LinkedIn at all.

Two modes:

1. ``search_urls()`` (always on, zero risk) - builds fully-filtered LinkedIn
   search deep links (title, geo, seniority, remote, date-posted) that land in
   your digest. You click, you browse, you Easy Apply. The agent still does the
   tailoring: run ``jobagent tailor --url <linkedin job url>`` on anything you
   like and it writes the CV and cover letter for you.

2. ``LinkedInSource`` (opt-in, ``sources.linkedin.enabled: true``) - drives
   *your own* logged-in Chromium profile to read the search results you would
   have seen yourself. This is automated access to LinkedIn and it is against
   their terms. It is here because you asked for it; it is off by default, it
   throttles hard, and the risk of a restricted account is yours to accept.

Note that in practice you lose very little by leaving mode 2 off: most roles on
LinkedIn are syndicated from an ATS, so the Greenhouse/Lever/Adzuna sources
surface the same jobs with an application path the agent can actually drive.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Iterable
from urllib.parse import urlencode

from ..config import Config
from ..models import Job
from .base import Source, infer_country, is_remote, register, strip_html

log = logging.getLogger(__name__)

# LinkedIn geo ids for the countries we support.
GEO_IDS = {
    "IN": "102713980",   # India
    "GB": "101165590",   # United Kingdom
    "US": "103644278",   # United States
}

# f_E experience-level filter values.
EXPERIENCE = {"mid": "3", "senior": "4", "manager": "5", "director": "6"}

# f_WT workplace type: 1 on-site, 2 remote, 3 hybrid.
WORKPLACE = {"onsite": "1", "remote": "2", "hybrid": "3"}


def search_urls(config: Config) -> list[dict[str, str]]:
    """Build one pre-filtered LinkedIn search link per (title, country).

    Safe to call always: this only assembles URLs, it makes no requests.
    """
    urls: list[dict[str, str]] = []
    days = max(1, int(config.search.max_age_days))
    for country in config.search.countries:
        geo = GEO_IDS.get(country)
        if not geo:
            continue
        for title in config.search.titles:
            params = {
                "keywords": title,
                "geoId": geo,
                "f_TPR": f"r{days * 86400}",          # posted in the last N days
                "f_E": ",".join(EXPERIENCE[k] for k in ("senior", "manager", "director")),
                "sortBy": "DD",                        # most recent first
            }
            if config.search.remote_only:
                params["f_WT"] = WORKPLACE["remote"]
            urls.append({
                "country": country,
                "title": title,
                "url": "https://www.linkedin.com/jobs/search/?" + urlencode(params),
            })
    return urls


@register
class LinkedInSource(Source):
    """Opt-in browser-assisted LinkedIn reader. Off unless you turn it on.

    Uses the persistent Chromium profile you log into once
    (``jobagent login-browser``), so it never touches your password and never
    holds a session cookie of its own.
    """

    name = "linkedin"

    def fetch(self) -> Iterable[Job]:
        if not self.conf.get("acknowledge_tos_risk"):
            log.warning(
                "linkedin source is enabled but 'acknowledge_tos_risk: true' is not "
                "set - skipping. Automating LinkedIn breaches their User Agreement "
                "and can get your account restricted. Use the search links in your "
                "digest instead."
            )
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error("linkedin source needs playwright: pip install playwright && playwright install chromium")
            return

        max_per_search = int(self.conf.get("max_per_search", 25))
        profile_dir = self.config.apply.browser_profile_dir

        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                profile_dir,
                headless=bool(self.conf.get("headless", False)),
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            try:
                for search in search_urls(self.config):
                    try:
                        yield from self._scrape_search(page, search, max_per_search)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("LinkedIn search %s failed: %s", search["title"], exc)
                    # Deliberately slow: this is a human-paced read, not a crawl.
                    time.sleep(random.uniform(4.0, 9.0))
            finally:
                context.close()

    def _scrape_search(self, page, search: dict, limit: int) -> Iterable[Job]:
        page.goto(search["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)

        if "/authwall" in page.url or "/login" in page.url:
            log.error("LinkedIn wants a login. Run: jobagent login-browser")
            return

        cards = page.locator("div.job-card-container, li.jobs-search-results__list-item")
        count = min(cards.count(), limit)
        for i in range(count):
            card = cards.nth(i)
            try:
                card.click(timeout=8000)
                page.wait_for_timeout(random.randint(1500, 3000))
                title = _text(page, "h1.job-title, .job-details-jobs-unified-top-card__job-title")
                company = _text(page, ".job-details-jobs-unified-top-card__company-name")
                location = _text(page, ".job-details-jobs-unified-top-card__tertiary-description-container")
                description = _text(page, "#job-details, .jobs-description__content")
                url = page.url.split("?")[0]
                if not title or not company:
                    continue
                easy = page.locator("button.jobs-apply-button").count() > 0
                yield Job(
                    source=self.name,
                    title=title,
                    company=company,
                    url=url,
                    location=location,
                    country=infer_country(location) or search["country"],
                    remote=is_remote(location, title),
                    description=strip_html(description),
                    apply_method="assisted",
                    apply_meta={"easy_apply": easy},
                    raw={"search_title": search["title"]},
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("card %d skipped: %s", i, exc)
                continue


def _text(page, selector: str) -> str:
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return ""
        return (loc.inner_text(timeout=4000) or "").strip()
    except Exception:  # noqa: BLE001
        return ""
