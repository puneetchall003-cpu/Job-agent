import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from jobagent.config import from_dict
from jobagent.models import Job, Profile


@pytest.fixture
def config(tmp_path):
    return from_dict({
        "data_dir": str(tmp_path / "data"),
        "search": {
            "countries": ["IN", "GB", "US"],
            "titles": ["Lead DevOps Engineer", "Platform Engineering Lead"],
            "keywords": ["kubernetes", "terraform"],
            "min_salary": {"GB": 90000},
            "max_age_days": 21,
        },
        "match": {"min_score": 60, "use_llm": False},
        "apply": {"enabled": False, "daily_limit": 5},
        "sources": {},
        "profile": {"answers": {"notice_period": "60 days"}},
    })


@pytest.fixture
def profile():
    return Profile(
        full_name="Puneet Chall",
        email="puneet@example.com",
        phone="+91 98765 43210",
        headline="Lead DevOps Engineer",
        years_experience=11,
        skills=["kubernetes", "terraform", "aws", "python", "docker",
                "jenkins", "prometheus", "linux"],
        answers={"notice_period": "60 days"},
    )


def make_job(**kwargs):
    defaults = dict(
        source="test", title="Lead DevOps Engineer", company="Acme",
        url="https://example.com/job/1", location="London, UK", country="GB",
        description="We need Kubernetes, Terraform, AWS, Python, Docker, "
                    "Jenkins and Prometheus experience on Linux.",
    )
    defaults.update(kwargs)
    return Job(**defaults)
