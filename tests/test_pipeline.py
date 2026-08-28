"""End-to-end pipeline tests using a stub source and stub apply drivers."""
import pytest

from jobagent import apply as apply_pkg
from jobagent.apply.base import ApplyResult, BaseDriver
from jobagent.db import Store
from jobagent.llm import NullLLM
from jobagent.models import (
    STATUS_APPLIED,
    STATUS_QUEUED,
    STATUS_SKIPPED,
)
from jobagent.pipeline import Agent
from jobagent.sources.base import Source, register
from tests.conftest import make_job


class StubSource(Source):
    name = "stub"
    jobs: list = []

    def fetch(self):
        return list(self.jobs)


register(StubSource)


@pytest.fixture
def agent(config, profile, monkeypatch):
    config.sources = {"stub": True}
    StubSource.jobs = [
        make_job(company="Acme", apply_method="greenhouse",
                 apply_meta={"board_token": "acme", "job_id": "1"}),
        make_job(company="Beta", title="Platform Engineering Lead",
                 apply_method="assisted"),
        make_job(company="Gamma", title="Junior DevOps Engineer"),   # blocked
        make_job(company="Delta", title="Senior Accountant",
                 description="Ledgers and VAT."),                     # low score
    ]
    agent = Agent(config, store=Store(config.db_path), profile=profile, llm=NullLLM())
    yield agent
    agent.close()


class RecordingDriver(BaseDriver):
    """Stands in for a real driver; records what it was asked to submit."""
    method = "recording"
    submitted: list = []
    result = ApplyResult.success("ok", confirmation="REF123")

    def submit(self, app, profile, dry_run=True):
        RecordingDriver.submitted.append((app.fingerprint, dry_run))
        return RecordingDriver.result


@pytest.fixture(autouse=True)
def _reset_driver():
    RecordingDriver.submitted = []
    RecordingDriver.result = ApplyResult.success("ok", confirmation="REF123")


def _use_recording_driver(monkeypatch):
    monkeypatch.setattr(apply_pkg, "get_driver",
                        lambda method, config, tailor=None: RecordingDriver(config, tailor))
    import jobagent.pipeline as pipeline
    monkeypatch.setattr(pipeline, "get_driver",
                        lambda method, config, tailor=None: RecordingDriver(config, tailor))


def test_dry_run_submits_nothing(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    report = agent.run(dry_run=True)

    assert report.discovered == 4
    assert report.matched == 2                    # junior + accountant filtered out
    assert report.applied == 0
    assert report.queued == 2
    assert all(dry is True for _, dry in RecordingDriver.submitted)


def test_real_run_applies_and_records(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    agent.config.apply.enabled = True
    report = agent.run(dry_run=False)

    assert report.applied == 2
    applied = agent.store.by_status(STATUS_APPLIED)
    assert len(applied) == 2
    assert all(a.applied_at for a in applied)
    assert agent.store.applied_today() == 2


def test_second_run_skips_already_seen_jobs(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    agent.run(dry_run=True)
    RecordingDriver.submitted = []

    second = agent.run(dry_run=True)
    assert second.new == 0
    assert second.matched == 0
    assert RecordingDriver.submitted == []


def test_daily_limit_queues_the_overflow(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    agent.config.apply.enabled = True
    agent.config.apply.daily_limit = 1
    report = agent.run(dry_run=False)

    assert report.applied == 1
    assert report.queued == 1


def test_same_company_is_not_spammed(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    StubSource.jobs = [
        make_job(company="Acme", title="Lead DevOps Engineer"),
        make_job(company="Acme", title="Platform Engineering Lead"),
    ]
    agent.config.apply.enabled = True
    report = agent.run(dry_run=False)

    assert report.applied == 1
    assert report.skipped >= 1


def test_api_rejection_falls_back_to_assisted(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    RecordingDriver.result = ApplyResult.human("board needs an API key")
    agent.config.apply.enabled = True
    report = agent.run(dry_run=False)

    assert report.applied == 0
    queued = agent.store.by_status(STATUS_QUEUED)
    assert len(queued) == 2
    # A greenhouse job that the API refused must be rerouted to the browser flow.
    acme = next(a for a in queued if a.job.company == "Acme")
    assert acme.job.apply_method == "assisted"


def test_driver_crash_is_contained(agent, monkeypatch):
    class Exploding(BaseDriver):
        def submit(self, app, profile, dry_run=True):
            raise RuntimeError("driver blew up")

    import jobagent.pipeline as pipeline
    monkeypatch.setattr(pipeline, "get_driver",
                        lambda m, c, t=None: Exploding(c, t))
    agent.config.apply.enabled = True
    report = agent.run(dry_run=False)

    assert report.failed == 2
    assert report.applied == 0


def test_apply_disabled_queues_instead_of_submitting(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    agent.config.apply.enabled = False
    report = agent.run(dry_run=False)

    assert report.applied == 0 and report.queued == 2
    assert RecordingDriver.submitted == []


def test_confirmation_callback_can_decline(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    agent.config.apply.enabled = True
    report = agent.run(dry_run=False, confirm=lambda app: app.job.company == "Acme")

    assert report.applied == 1
    assert report.skipped >= 1
    assert any(a.job.company == "Beta"
               for a in agent.store.by_status(STATUS_SKIPPED))


def test_tailoring_writes_documents(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    agent.run(dry_run=True)
    files = list(agent.config.output_dir.rglob("cover-letter.md"))
    assert len(files) == 2
    assert files[0].read_text().strip()


def test_limit_caps_processing(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    report = agent.run(dry_run=True, limit=1)
    assert report.tailored == 1


def test_process_queue_works_the_backlog(agent, monkeypatch):
    _use_recording_driver(monkeypatch)
    agent.run(dry_run=True)                       # leaves 2 queued
    agent.config.apply.enabled = True
    report = agent.process_queue(dry_run=False, limit=10)
    assert report.applied >= 1
