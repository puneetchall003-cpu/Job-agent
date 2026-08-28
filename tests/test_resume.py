import pytest

from jobagent.resume import _json_from, extract_text, parse_heuristic

SAMPLE = """Puneet Chall
puneet@example.com | +91 98765 43210 | linkedin.com/in/puneetchall | github.com/pchall
Bengaluru, India

Lead DevOps Engineer with 11 years of experience.

EXPERIENCE
Lead DevOps Engineer, Acme Corp, 2019 - Present
- Kubernetes, Terraform and AWS at scale; ArgoCD GitOps; Prometheus and Grafana
- Python and Go automation, Jenkins and GitHub Actions pipelines
Senior Systems Engineer, Beta Ltd, 2014 - 2019

CERTIFICATIONS
AWS Certified Solutions Architect
CKA
"""


def test_contact_details():
    p = parse_heuristic(SAMPLE)
    assert p.full_name == "Puneet Chall"
    assert p.email == "puneet@example.com"
    assert p.phone == "+91 98765 43210"
    assert p.linkedin_url == "https://linkedin.com/in/puneetchall"
    assert p.github_url == "https://github.com/pchall"


def test_skills_and_experience():
    p = parse_heuristic(SAMPLE)
    for skill in ["kubernetes", "terraform", "aws", "argocd", "python", "jenkins"]:
        assert skill in p.skills
    assert p.years_experience == 11.0


def test_certifications_and_titles():
    p = parse_heuristic(SAMPLE)
    assert "CKA" in p.certifications
    assert any("AWS Certified" in c for c in p.certifications)
    assert any("Devops" in t for t in p.titles)


def test_extra_skills_are_recognised():
    p = parse_heuristic(SAMPLE + "\nFinOps and platform engineering.",
                        extra_skills=["finops"])
    assert "finops" in p.skills


def test_empty_resume_does_not_crash():
    p = parse_heuristic("")
    assert p.skills == [] and p.full_name == ""


def test_extract_text_txt(tmp_path):
    path = tmp_path / "cv.txt"
    path.write_text(SAMPLE)
    assert "Puneet Chall" in extract_text(path)


def test_extract_text_docx(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "cv.docx"
    document = docx.Document()
    for line in SAMPLE.splitlines():
        document.add_paragraph(line)
    document.save(str(path))
    text = extract_text(path)
    assert "Puneet Chall" in text and "Kubernetes" in text


def test_extract_text_rejects_unknown_format(tmp_path):
    path = tmp_path / "cv.rtf"
    path.write_text("x")
    with pytest.raises(ValueError):
        extract_text(path)


def test_extract_text_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_text(tmp_path / "nope.pdf")


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Sure! Here it is: {"a": 1} hope that helps', {"a": 1}),
    ("no json here", {}),
    ("", {}),
])
def test_json_extraction_survives_chatty_models(raw, expected):
    assert _json_from(raw) == expected
