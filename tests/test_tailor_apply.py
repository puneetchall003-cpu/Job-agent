import pytest

from jobagent.apply import get_driver
from jobagent.apply.assisted import AssistedDriver
from jobagent.apply.ats_api import GreenhouseDriver, LeverDriver
from jobagent.llm import LLM, LLMError, NullLLM
from jobagent.models import Application
from jobagent.tailor import Tailor, _clean_letter, _match_known_answer, _slug
from tests.conftest import make_job


class ScriptedLLM(LLM):
    """LLM that returns canned responses instead of calling the API."""

    def __init__(self, response):
        super().__init__(api_key="fake")
        self.response = response
        self.prompts = []

    @property
    def available(self):
        return True

    def complete(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.response


# --- known answers -------------------------------------------------------------
@pytest.mark.parametrize("question,key", [
    ("What is your notice period?", "notice_period"),
    ("When can you start?", "notice_period"),
    ("Will you require visa sponsorship?", "work_authorization"),
    ("Are you legally authorized to work in the US?", "work_authorization"),
    ("What are your salary expectations?", "salary_expectation"),
    ("Expected CTC?", "salary_expectation"),
    ("Are you willing to relocate?", "relocation"),
    ("LinkedIn profile URL", "linkedin_url"),
])
def test_known_answers_match_by_intent(question, key):
    known = {key: "ANSWER"}
    assert _match_known_answer(question, known) == "ANSWER"


def test_unknown_question_has_no_canned_answer():
    assert _match_known_answer("Describe a difficult outage", {"notice_period": "x"}) == ""


def test_sensitive_questions_never_go_to_the_llm(config, profile):
    """Visa and notice period must come from config, not be invented."""
    llm = ScriptedLLM("the model should never be asked this")
    profile.answers = {"work_authorization": "Requires sponsorship"}
    tailor = Tailor(config, profile, llm)
    answer = tailor.answer(make_job(), "Do you require visa sponsorship?")
    assert answer == "Requires sponsorship"
    assert llm.prompts == []


def test_needs_human_response_yields_no_answer(config, profile):
    tailor = Tailor(config, profile, ScriptedLLM("NEEDS_HUMAN"))
    assert tailor.answer(make_job(), "What is your current base salary?") == ""


def test_answer_is_truncated_to_the_form_limit(config, profile):
    tailor = Tailor(config, profile, ScriptedLLM("x" * 5000))
    assert len(tailor.answer(make_job(), "Tell us about yourself", limit=200)) == 200


# --- cover letters -------------------------------------------------------------
def test_fallback_letter_without_an_api_key(config, profile):
    tailor = Tailor(config, profile, NullLLM())
    letter = tailor.cover_letter(make_job())
    assert "Acme" in letter and "Lead DevOps Engineer" in letter
    assert profile.full_name in letter


def test_llm_letter_is_cleaned_up(config, profile):
    tailor = Tailor(config, profile, ScriptedLLM(
        "Here's the cover letter:\n\nDear team — I am applying."))
    letter = tailor.cover_letter(make_job())
    assert not letter.lower().startswith("here")
    assert "—" not in letter


def test_letter_falls_back_when_the_llm_errors(config, profile):
    class Broken(ScriptedLLM):
        def complete(self, *a, **k):
            raise LLMError("rate limited")

    letter = Tailor(config, profile, Broken("")).cover_letter(make_job())
    assert "Acme" in letter          # fell back to the template, did not crash


def test_prepare_writes_the_document_bundle(config, profile):
    tailor = Tailor(config, profile, NullLLM())
    job = make_job()
    app = Application(fingerprint=job.fingerprint, job=job, score=88.0)
    tailor.prepare(app)

    folder = next(config.output_dir.iterdir())
    names = {p.name for p in folder.iterdir()}
    assert {"cover-letter.md", "job.md"} <= names
    assert "Acme" in (folder / "job.md").read_text()


def test_tailored_resume_parses_llm_json(config, profile):
    tailor = Tailor(config, profile, ScriptedLLM(
        '{"summary":"S","skills":["kubernetes"],"bullets":["Did a thing"]}'))
    profile.resume_text = "some resume text"
    out = tailor.tailored_resume(make_job())
    assert "## Summary" in out and "kubernetes" in out and "Did a thing" in out


def test_slug_is_filesystem_safe():
    assert _slug("Acme Corp / Lead DevOps (UK)") == "acme-corp-lead-devops-uk"
    assert _slug("!!!") == "job"


def test_clean_letter_strips_fences():
    assert _clean_letter("```\nDear team\n```") == "Dear team"


# --- drivers -------------------------------------------------------------------
def test_driver_selection():
    from jobagent.config import from_dict
    config = from_dict({})
    assert isinstance(get_driver("greenhouse", config), GreenhouseDriver)
    assert isinstance(get_driver("lever", config), LeverDriver)
    assert isinstance(get_driver("assisted", config), AssistedDriver)
    # Anything unrecognised must still be actionable, not dropped.
    assert isinstance(get_driver("workday", config), AssistedDriver)


def test_dry_run_never_posts(config, profile, tmp_path, monkeypatch):
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    profile.resume_path = str(resume)

    def explode(*a, **k):
        raise AssertionError("dry run must not make a network call")

    monkeypatch.setattr("jobagent.apply.ats_api.requests.post", explode)

    job = make_job(apply_method="greenhouse",
                   apply_meta={"board_token": "acme", "job_id": "1"})
    app = Application(fingerprint=job.fingerprint, job=job, cover_letter="Dear team")
    result = GreenhouseDriver(config).submit(app, profile, dry_run=True)
    assert result.ok and "DRY RUN" in result.message


def test_missing_resume_requires_a_human(config, profile):
    job = make_job(apply_method="lever", apply_meta={"site": "a", "posting_id": "b"})
    app = Application(fingerprint=job.fingerprint, job=job)
    result = LeverDriver(config).submit(app, profile, dry_run=False)
    assert result.needs_human and "resume" in result.message


def test_missing_apply_metadata_requires_a_human(config, profile):
    job = make_job(apply_method="greenhouse", apply_meta={})
    app = Application(fingerprint=job.fingerprint, job=job)
    assert GreenhouseDriver(config).submit(app, profile, dry_run=False).needs_human


def test_greenhouse_auth_failure_routes_to_the_browser(config, profile, tmp_path, monkeypatch):
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4")
    profile.resume_path = str(resume)

    class Response:
        status_code = 401
        text = "unauthorized"

    monkeypatch.setattr("jobagent.apply.ats_api.requests.post", lambda *a, **k: Response())

    job = make_job(apply_method="greenhouse",
                   apply_meta={"board_token": "acme", "job_id": "1"})
    app = Application(fingerprint=job.fingerprint, job=job)
    result = GreenhouseDriver(config).submit(app, profile, dry_run=False)
    assert result.needs_human and not result.ok


def test_name_splitting():
    from jobagent.models import Profile
    driver = GreenhouseDriver.__new__(GreenhouseDriver)
    assert driver.split_name(Profile(full_name="Puneet Chall")) == ("Puneet", "Chall")
    assert driver.split_name(Profile(full_name="Ana Maria De Souza")) == ("Ana", "Maria De Souza")
    assert driver.split_name(Profile(full_name="Cher")) == ("Cher", "")
    assert driver.split_name(Profile(full_name="")) == ("", "")


def test_null_llm_is_unavailable_and_raises():
    llm = NullLLM()
    assert not llm.available
    with pytest.raises(LLMError):
        llm.complete("hi")
