"""End-to-end orchestration: discover -> score -> tailor -> apply."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from .apply import get_driver
from .config import Config
from .db import Store
from .llm import build_llm
from .matching import llm_rescore, score_job
from .models import (
    Application,
    Job,
    Profile,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_MATCHED,
    STATUS_QUEUED,
    STATUS_SKIPPED,
    STATUS_TAILORED,
)
from .resume import build_profile
from .sources import HttpClient, build_sources
from .tailor import Tailor

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    discovered: int = 0
    new: int = 0
    matched: int = 0
    tailored: int = 0
    applied: int = 0
    queued: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    applications: list[Application] = field(default_factory=list)
    per_source: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"discovered={self.discovered} new={self.new} matched={self.matched} "
            f"tailored={self.tailored} applied={self.applied} queued={self.queued} "
            f"failed={self.failed}"
        )


class Agent:
    """The whole thing. Construct once, call `run()`."""

    def __init__(self, config: Config, store: Optional[Store] = None,
                 profile: Optional[Profile] = None, llm=None):
        self.config = config
        self.store = store or Store(config.db_path)
        self.llm = llm if llm is not None else build_llm(config)
        self.profile = profile or build_profile(config, self.llm)
        self.tailor = Tailor(config, self.profile, self.llm)

    def close(self) -> None:
        self.store.close()

    # --- stage 1: discovery ----------------------------------------------------
    def discover(self) -> list[Job]:
        """Pull from every enabled source and dedupe within the batch."""
        http = HttpClient(delay=float(self.config.raw.get("http_delay", 0.6)))
        jobs: list[Job] = []
        seen: set[str] = set()

        for source in build_sources(self.config, http):
            found = source.collect()
            self._report.per_source[source.name] = len(found)
            for job in found:
                if job.fingerprint in seen:
                    continue
                seen.add(job.fingerprint)
                jobs.append(job)
        log.info("Discovered %d unique jobs from %d sources",
                 len(jobs), len(self._report.per_source))
        return jobs

    # --- stage 2: matching -----------------------------------------------------
    def evaluate(self, jobs: list[Job]) -> list[Application]:
        """Score every new job and persist a verdict for each one.

        Every fresh job gets exactly one row, including the ones that are
        rejected outright - otherwise the agent would rescore the same junior
        postings on every single run and never remember saying no.
        """
        fresh = [j for j in jobs if not self.store.known(j.fingerprint)]
        self._report.new = len(fresh)
        log.info("%d of %d jobs are new", len(fresh), len(jobs))

        scored = [(job, score_job(self.config, self.profile, job)) for job in fresh]
        scored.sort(key=lambda pair: pair[1].score, reverse=True)

        use_llm = self.config.match.use_llm and self.llm.available
        llm_used = 0
        matched: list[Application] = []

        for job, result in scored:
            app = self.store.upsert_job(job)
            app.score = result.score
            app.match_reasons = result.reasons

            if not result.passed:
                self._skip(app, "; ".join(result.blockers))
                continue

            if result.score < self.config.match.min_score:
                self._skip(app, f"score {result.score} below {self.config.match.min_score}")
                continue

            # Only the best candidates are worth an LLM call.
            if use_llm and llm_used < self.config.match.llm_top_n:
                llm_used += 1
                try:
                    result = llm_rescore(self.config, self.profile, job, result, self.llm)
                    app.score = result.score
                    app.match_reasons = result.reasons
                except Exception as exc:  # noqa: BLE001 - keep the keyword score
                    log.warning("LLM screen failed for %s: %s", job.title, exc)

            threshold = (self.config.match.llm_min_score
                         if result.llm_score is not None else self.config.match.min_score)
            if not result.passed or result.score < threshold:
                self._skip(app, "; ".join(result.blockers)
                           or f"score {result.score} below {threshold}")
                continue

            app.touch(STATUS_MATCHED)
            self.store.save(app)
            matched.append(app)

        self._report.matched = len(matched)
        log.info("%d matched, %d screened out", len(matched), self._report.skipped)
        return matched

    def _skip(self, app: Application, reason: str) -> None:
        app.error = reason
        app.touch(STATUS_SKIPPED)
        self.store.save(app)
        self._report.skipped += 1

    # --- stage 3: tailoring ----------------------------------------------------
    def prepare(self, apps: list[Application]) -> list[Application]:
        prepared = []
        for app in apps:
            try:
                self.tailor.prepare(app)
                app.touch(STATUS_TAILORED)
                self.store.save(app)
                prepared.append(app)
                self._report.tailored += 1
            except Exception as exc:  # noqa: BLE001
                log.error("Tailoring failed for %s: %s", app.job.company, exc)
                app.error = f"tailoring failed: {exc}"
                app.touch(STATUS_FAILED)
                self.store.save(app)
                self._report.failed += 1
                self._report.errors.append(f"{app.job.company}: {exc}")
        return prepared

    # --- stage 4: applying -----------------------------------------------------
    def apply(self, apps: list[Application], dry_run: bool = True,
              confirm: Optional[Callable[[Application], bool]] = None) -> None:
        """Submit or queue each application, respecting the daily limit."""
        budget = self.config.apply.daily_limit - self.store.applied_today()
        if budget <= 0:
            log.info("Daily limit of %d already reached", self.config.apply.daily_limit)
            self._queue_all(apps, "daily limit reached")
            return

        auto = set(self.config.apply.auto_methods)

        for app in apps:
            if budget <= 0:
                self._queue_all([app], "daily limit reached")
                continue

            method = app.job.apply_method
            interactive = method not in auto

            if self.store.applied_to_company_recently(app.job.company):
                app.error = "already applied to this company in the last 14 days"
                app.touch(STATUS_SKIPPED)
                self.store.save(app)
                self._report.skipped += 1
                continue

            if not self.config.apply.enabled and not dry_run:
                self._queue_all([app], "apply.enabled is false")
                continue

            if confirm is not None and not dry_run and not confirm(app):
                app.error = "declined at confirmation"
                app.touch(STATUS_SKIPPED)
                self.store.save(app)
                self._report.skipped += 1
                continue

            driver = get_driver(method, self.config, self.tailor)
            try:
                result = driver.submit(app, self.profile, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001
                log.error("Apply driver crashed for %s: %s", app.job.company, exc)
                result = None
                app.error = f"driver crashed: {exc}"

            if result is None:
                app.touch(STATUS_FAILED)
                self._report.failed += 1
            elif result.ok:
                app.error = result.confirmation or result.message
                app.touch(STATUS_APPLIED if not dry_run else STATUS_QUEUED)
                if not dry_run:
                    self._report.applied += 1
                    budget -= 1
                else:
                    self._report.queued += 1
            elif result.details.get("skip"):
                app.error = result.message
                app.touch(STATUS_SKIPPED)
                self._report.skipped += 1
            elif result.needs_human:
                # The API path was refused; the browser flow can still do it.
                if not interactive:
                    app.job.apply_method = "assisted"
                app.error = result.message
                app.touch(STATUS_QUEUED)
                self._report.queued += 1
            else:
                app.error = result.message
                app.touch(STATUS_FAILED)
                self._report.failed += 1
                self._report.errors.append(f"{app.job.company}: {result.message}")

            self.store.save(app)
            self._report.applications.append(app)

    def _queue_all(self, apps: list[Application], reason: str) -> None:
        for app in apps:
            app.error = reason
            app.touch(STATUS_QUEUED)
            self.store.save(app)
            self._report.queued += 1

    # --- top level -------------------------------------------------------------
    def run(self, dry_run: bool = True, limit: Optional[int] = None,
            confirm: Optional[Callable[[Application], bool]] = None) -> RunReport:
        self._report = RunReport()
        run_id = self.store.start_run()

        jobs = self.discover()
        self._report.discovered = len(jobs)

        matched = self.evaluate(jobs)
        if limit:
            matched = matched[:limit]

        prepared = self.prepare(matched)
        self.apply(prepared, dry_run=dry_run, confirm=confirm)

        self.store.finish_run(
            run_id, self._report.discovered, self._report.matched,
            self._report.applied, notes=self._report.summary(),
        )
        return self._report

    def process_queue(self, dry_run: bool = False, limit: int = 10,
                      confirm: Optional[Callable[[Application], bool]] = None) -> RunReport:
        """Work through jobs already queued for the browser-assisted flow."""
        self._report = RunReport()
        queued = self.store.by_status(STATUS_QUEUED, STATUS_TAILORED, limit=limit)
        self._report.matched = len(queued)
        self.apply(queued, dry_run=dry_run, confirm=confirm)
        return self._report
