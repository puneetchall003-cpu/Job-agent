from datetime import datetime, timedelta, timezone

from jobagent.matching import parse_date, rank, score_job
from tests.conftest import make_job


def test_strong_match_scores_high(config, profile):
    result = score_job(config, profile, make_job())
    assert result.passed
    assert result.score >= 80
    assert any("matching skills" in r for r in result.reasons)


def test_junior_titles_are_blocked(config, profile):
    for title in ["Junior DevOps Engineer", "DevOps Intern",
                  "Graduate Platform Engineer", "Entry-Level SRE"]:
        result = score_job(config, profile, make_job(title=title))
        assert not result.passed, title


def test_excluded_company_blocked(config, profile):
    config.search.exclude_companies = ["Acme"]
    assert not score_job(config, profile, make_job()).passed


def test_country_outside_targets_blocked(config, profile):
    result = score_job(config, profile, make_job(location="Berlin", country="DE"))
    assert not result.passed


def test_unresolved_location_blocked_unless_remote(config, profile):
    assert not score_job(config, profile, make_job(location="", country="")).passed
    ok = score_job(config, profile, make_job(location="", country="", remote=True))
    assert ok.passed


def test_remote_only_blocks_onsite(config, profile):
    config.search.remote_only = True
    assert not score_job(config, profile, make_job(remote=False)).passed
    assert score_job(config, profile, make_job(remote=True)).passed


def test_stale_posting_blocked(config, profile):
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    assert not score_job(config, profile, make_job(posted_at=old)).passed
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert score_job(config, profile, make_job(posted_at=recent)).passed


def test_low_salary_reduces_but_does_not_block(config, profile):
    low = score_job(config, profile, make_job(salary_max=50000, salary_currency="GBP"))
    high = score_job(config, profile, make_job(salary_max=140000, salary_currency="GBP"))
    assert low.passed and high.passed
    assert high.score > low.score


def test_unknown_salary_is_neutral(config, profile):
    known = score_job(config, profile, make_job(salary_max=140000))
    unknown = score_job(config, profile, make_job())
    assert abs(known.score - unknown.score) <= 5


def test_irrelevant_role_scores_below_threshold(config, profile):
    job = make_job(title="Senior Accountant",
                   description="Manage ledgers, VAT returns and payroll.")
    result = score_job(config, profile, job)
    assert result.score < config.match.min_score


def test_rank_sorts_and_drops_blocked(config, profile):
    jobs = [
        make_job(title="Junior DevOps Engineer"),
        make_job(title="Platform Engineering Lead", company="Beta"),
        make_job(title="Lead DevOps Engineer", company="Gamma"),
    ]
    ranked = rank(config, profile, jobs)
    assert len(ranked) == 2
    assert ranked[0][1].score >= ranked[1][1].score


def test_parse_date_formats():
    assert parse_date("2025-01-15T10:00:00Z").year == 2025
    assert parse_date("2025-01-15").month == 1
    assert parse_date(None) is None
    assert parse_date("not a date") is None
    assert parse_date("2025-01-15").tzinfo is not None
