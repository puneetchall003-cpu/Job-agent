import pytest

from jobagent.sources import available_sources, build_sources
from jobagent.sources.base import HttpClient, infer_country, is_remote, strip_html
from jobagent.sources.aggregators import RemotiveSource
from jobagent.sources.ats import GreenhouseSource, LeverSource
from jobagent.sources.linkedin import search_urls


class FakeHttp(HttpClient):
    """HttpClient that answers from a canned map instead of the network."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append(url)
        for fragment, payload in self.payloads.items():
            if fragment in url:
                return payload
        return None


@pytest.mark.parametrize("text,expected", [
    ("Bengaluru, India", "IN"), ("Gurgaon", "IN"), ("London, UK", "GB"),
    ("Edinburgh", "GB"), ("Austin, TX", "US"), ("San Francisco", "US"),
    ("New York, NY", "US"), ("Berlin, Germany", ""), ("", ""),
])
def test_country_inference(text, expected):
    assert infer_country(text) == expected


def test_remote_detection():
    assert is_remote("Remote - UK") and is_remote("Work from home")
    assert not is_remote("London office")


def test_strip_html():
    out = strip_html("<div><p>Hello<br>World</p><script>bad()</script>&amp;co</div>")
    assert "bad()" not in out and "Hello" in out and "&co" in out


def test_all_sources_registered():
    assert set(available_sources()) >= {
        "adzuna", "remotive", "remoteok", "arbeitnow", "jobicy",
        "greenhouse", "lever", "ashby", "linkedin"}


def test_build_sources_respects_enabled_flags(config):
    config.sources = {"remotive": True, "remoteok": {"enabled": False}}
    names = {s.name for s in build_sources(config)}
    assert names == {"remotive"}


def test_greenhouse_parsing(config):
    http = FakeHttp({"boards/acme/jobs": {"jobs": [{
        "id": 4242, "title": "Lead DevOps Engineer",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/4242",
        "location": {"name": "London, UK"},
        "content": "<p>Kubernetes and Terraform</p>",
        "updated_at": "2025-01-10T00:00:00Z",
        "departments": [{"name": "Infrastructure"}],
    }]}})
    config.sources = {"greenhouse": {"companies": ["acme"]}}
    jobs = GreenhouseSource(config, http).collect()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Lead DevOps Engineer"
    assert job.country == "GB"
    assert job.apply_method == "greenhouse"
    assert job.apply_meta == {"board_token": "acme", "job_id": "4242"}
    assert "Kubernetes" in job.description and "<p>" not in job.description


def test_lever_parsing(config):
    http = FakeHttp({"postings/acme": [{
        "id": "abc-123", "text": "Staff SRE",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "categories": {"location": "Bengaluru", "team": "Infra"},
        "workplaceType": "remote",
        "descriptionPlain": "Terraform, AWS",
        "createdAt": 1700000000000,
    }]})
    config.sources = {"lever": {"companies": ["acme"]}}
    job = LeverSource(config, http).collect()[0]

    assert job.title == "Staff SRE" and job.country == "IN" and job.remote
    assert job.apply_method == "lever"
    assert job.apply_meta == {"site": "acme", "posting_id": "abc-123"}
    assert job.posted_at.startswith("2023-11")


def test_remotive_parsing(config):
    http = FakeHttp({"remote-jobs": {"jobs": [{
        "title": "DevOps Lead", "company_name": "Beta",
        "url": "https://remotive.com/j/1",
        "candidate_required_location": "USA",
        "description": "<b>Kubernetes</b>",
        "publication_date": "2025-02-01T00:00:00",
    }]}})
    config.sources = {"remotive": {"search_terms": ["devops"]}}
    job = RemotiveSource(config, http).collect()[0]
    assert job.remote and job.country == "US" and job.description == "Kubernetes"


def test_source_failure_is_contained(config):
    class Exploding(GreenhouseSource):
        def fetch(self):
            raise RuntimeError("boom")

    config.sources = {"greenhouse": {"companies": ["acme"]}}
    assert Exploding(config, FakeHttp({})).collect() == []


def test_one_bad_board_does_not_stop_the_others(config):
    class PartlyBroken(GreenhouseSource):
        def fetch_company(self, token):
            if token == "bad":
                raise RuntimeError("404")
            yield from super().fetch_company(token)

    http = FakeHttp({"boards/good/jobs": {"jobs": [{
        "id": 1, "title": "Lead DevOps Engineer",
        "absolute_url": "https://x", "location": {"name": "London"},
        "content": "k8s",
    }]}})
    config.sources = {"greenhouse": {"companies": ["bad", "good"]}}
    assert len(PartlyBroken(config, http).collect()) == 1


def test_linkedin_urls_are_built_not_fetched(config):
    urls = search_urls(config)
    assert len(urls) == len(config.search.countries) * len(config.search.titles)
    assert all(u["url"].startswith("https://www.linkedin.com/jobs/search/") for u in urls)
    assert any("geoId=102713980" in u["url"] for u in urls)  # India


def test_linkedin_source_refuses_without_acknowledgement(config):
    from jobagent.sources.linkedin import LinkedInSource
    config.sources = {"linkedin": {"enabled": True, "acknowledge_tos_risk": False}}
    assert LinkedInSource(config, FakeHttp({})).collect() == []


def test_jobicy_falls_back_to_unfiltered_on_bad_industry(config):
    from jobagent.sources.aggregators import JobicySource

    class RejectsIndustry(FakeHttp):
        def get_json(self, url, params=None, **kwargs):
            self.calls.append(params)
            if params and "industry" in params:
                return None            # Jobicy answers 400 for unknown slugs
            return {"jobs": [{"jobTitle": "DevOps Lead", "companyName": "X",
                              "url": "https://jobicy.com/j/1", "jobGeo": "USA"}]}

    http = RejectsIndustry({})
    config.sources = {"jobicy": {"industry": "not-a-real-slug"}}
    jobs = JobicySource(config, http).collect()
    assert len(jobs) == 1
    assert len(http.calls) == 2 and "industry" not in http.calls[1]
