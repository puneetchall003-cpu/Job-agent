from datetime import datetime, timedelta, timezone

from jobagent.db import Store
from jobagent.models import STATUS_APPLIED, STATUS_QUEUED
from tests.conftest import make_job


def test_upsert_is_idempotent(config):
    with Store(config.db_path) as store:
        job = make_job()
        first = store.upsert_job(job)
        second = store.upsert_job(job)
        assert first.fingerprint == second.fingerprint
        assert len(store.recent()) == 1


def test_fingerprint_dedupes_across_sources(config):
    """The same role from LinkedIn and Greenhouse must collapse into one row."""
    with Store(config.db_path) as store:
        store.upsert_job(make_job(source="greenhouse", url="https://gh/1"))
        store.upsert_job(make_job(source="linkedin", url="https://li/9"))
        assert len(store.recent()) == 1


def test_roundtrip_preserves_job(config):
    with Store(config.db_path) as store:
        job = make_job(apply_method="lever",
                       apply_meta={"site": "acme", "posting_id": "x1"},
                       salary_max=120000)
        app = store.upsert_job(job)
        app.cover_letter = "Dear team"
        app.match_reasons = ["kubernetes"]
        app.touch(STATUS_QUEUED)
        store.save(app)

        loaded = store.get(job.fingerprint)
        assert loaded.job.apply_meta["posting_id"] == "x1"
        assert loaded.job.salary_max == 120000
        assert loaded.cover_letter == "Dear team"
        assert loaded.match_reasons == ["kubernetes"]
        assert loaded.status == STATUS_QUEUED


def test_applied_today_and_company_guard(config):
    with Store(config.db_path) as store:
        app = store.upsert_job(make_job())
        app.touch(STATUS_APPLIED)
        store.save(app)
        assert store.applied_today() == 1
        assert store.applied_to_company_recently("acme")     # case-insensitive
        assert not store.applied_to_company_recently("Other Co")


def test_company_guard_expires(config):
    with Store(config.db_path) as store:
        app = store.upsert_job(make_job())
        app.status = STATUS_APPLIED
        app.applied_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        store.save(app)
        assert not store.applied_to_company_recently("Acme", days=14)


def test_by_status_orders_by_score(config):
    with Store(config.db_path) as store:
        for i, company in enumerate(["A", "B", "C"]):
            app = store.upsert_job(make_job(company=company))
            app.score = i * 10
            app.touch(STATUS_QUEUED)
            store.save(app)
        queued = store.by_status(STATUS_QUEUED)
        assert [a.job.company for a in queued] == ["C", "B", "A"]


def test_run_bookkeeping(config):
    with Store(config.db_path) as store:
        run_id = store.start_run()
        store.finish_run(run_id, discovered=10, matched=3, applied=1, notes="ok")
        row = store._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        assert row["discovered"] == 10 and row["applied"] == 1 and row["ended_at"]
