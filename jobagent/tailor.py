"""Per-application document generation: cover letter, resume slant, form answers.

Everything here is grounded in the resume text. The prompts forbid inventing
experience, and `answers` falls back to your configured defaults rather than
letting the model guess at things like visa status or notice period.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import Config
from .llm import LLMError
from .models import Application, Job, Profile
from .resume import _json_from

log = logging.getLogger(__name__)

COVER_LETTER_PROMPT = """Write a cover letter for this application.

RULES
- 200-280 words, 3-4 short paragraphs, no bullet points.
- Every claim must be traceable to the resume below. Never invent an employer, \
a metric, a certification or a technology the candidate has not used.
- Open with the specific role and one concrete reason this company is interesting \
based on the job description - not flattery.
- Middle: two or three achievements from the resume that map directly onto what \
the job asks for. Use the numbers that are already in the resume.
- Close with a short, confident line. No "I look forward to hearing from you".
- Plain professional English. No em dashes, no buzzword stacking, no "passionate", \
no "I am writing to apply".
- Output the letter body only: no subject line, no address block, no signature.

CANDIDATE RESUME
{resume}

JOB
{title} at {company} ({location})
{description}
"""

RESUME_TAILOR_PROMPT = """Rewrite this candidate's resume summary and skills \
section so it leads with what this specific job asks for.

RULES
- Use ONLY facts present in the resume. Reorder and re-emphasise; never add.
- Summary: 3 sentences max, first person implied (no "I").
- Skills: pick the 12-16 most relevant to this job, most relevant first.
- Bullets: rewrite the 5 strongest achievement bullets from the resume so the \
wording matches the job's vocabulary, keeping every number intact.

Return ONLY JSON:
{{"summary": "...", "skills": ["..."], "bullets": ["...", ...]}}

RESUME
{resume}

JOB
{title} at {company}
{description}
"""

ANSWER_PROMPT = """Answer this job application question as the candidate.

RULES
- Ground every answer in the resume. Do not invent facts.
- If the question asks something only the candidate can know (visa status, \
notice period, salary, willingness to relocate) and it is not in the known \
answers below, respond with exactly: NEEDS_HUMAN
- Keep it under {limit} characters. Direct, no preamble.

KNOWN ANSWERS (authoritative, use verbatim where they fit)
{known}

RESUME
{resume}

JOB: {title} at {company}

QUESTION: {question}
"""

FALLBACK_LETTER = """Dear Hiring Team,

I am applying for the {title} role at {company}. I am a {headline} with \
{years} years of experience across {skills}.

In my recent roles I have owned production infrastructure end to end: designing \
and running the platforms teams ship on, automating delivery, and keeping \
services reliable at scale. The responsibilities in this posting line up \
closely with the work I do day to day.

My resume has the detail. I would welcome the chance to talk about how I can \
help {company}.

Best regards,
{name}
"""


class Tailor:
    def __init__(self, config: Config, profile: Profile, llm):
        self.config = config
        self.profile = profile
        self.llm = llm

    # --- cover letter ----------------------------------------------------------
    def cover_letter(self, job: Job) -> str:
        if self.llm.available:
            try:
                text = self.llm.complete(
                    COVER_LETTER_PROMPT.format(
                        resume=self.profile.resume_text[:12000],
                        title=job.title, company=job.company,
                        location=job.location or ("Remote" if job.remote else "-"),
                        description=(job.description or "")[:6000],
                    ),
                    max_tokens=900,
                    system="You are an experienced technical hiring writer. "
                           "You never fabricate candidate experience.",
                    temperature=0.5,
                )
                if text:
                    return _clean_letter(text)
            except LLMError as exc:
                log.warning("Cover letter generation failed for %s: %s", job.company, exc)
        return self._fallback_letter(job)

    def _fallback_letter(self, job: Job) -> str:
        return FALLBACK_LETTER.format(
            title=job.title, company=job.company,
            headline=self.profile.headline or "DevOps and platform engineer",
            years=int(self.profile.years_experience or 0) or "several",
            skills=", ".join(self.profile.skills[:6]) or "cloud infrastructure",
            name=self.profile.full_name or "",
        ).strip()

    # --- resume slant ----------------------------------------------------------
    def tailored_resume(self, job: Job) -> str:
        """A markdown block you can paste over the top of your CV for this role."""
        if not self.llm.available or not self.profile.resume_text:
            return ""
        try:
            raw = self.llm.complete(
                RESUME_TAILOR_PROMPT.format(
                    resume=self.profile.resume_text[:14000],
                    title=job.title, company=job.company,
                    description=(job.description or "")[:5000],
                ),
                max_tokens=1400,
                system="You re-emphasise existing resume content. You never add new facts.",
            )
        except LLMError as exc:
            log.warning("Resume tailoring failed for %s: %s", job.company, exc)
            return ""

        data = _json_from(raw)
        if not data:
            return ""
        parts = []
        if data.get("summary"):
            parts.append(f"## Summary\n\n{data['summary']}")
        if data.get("skills"):
            parts.append("## Key skills\n\n" + " | ".join(str(s) for s in data["skills"]))
        if data.get("bullets"):
            bullets = "\n".join(f"- {b}" for b in data["bullets"])
            parts.append(f"## Selected achievements\n\n{bullets}")
        return "\n\n".join(parts)

    # --- application questions -------------------------------------------------
    def answer(self, job: Job, question: str, limit: int = 600) -> str:
        """Answer one application question, or '' if a human must.

        Config answers are checked first and returned verbatim - the model is
        never asked to guess your visa status or notice period.
        """
        known = self.profile.answers or {}
        if direct := _match_known_answer(question, known):
            return direct

        if not self.llm.available:
            return ""
        try:
            text = self.llm.complete(
                ANSWER_PROMPT.format(
                    limit=limit,
                    known="\n".join(f"- {k}: {v}" for k, v in known.items()) or "(none)",
                    resume=self.profile.resume_text[:8000],
                    title=job.title, company=job.company, question=question,
                ),
                max_tokens=600,
            )
        except LLMError:
            return ""
        text = (text or "").strip()
        if not text or "NEEDS_HUMAN" in text:
            return ""
        return text[:limit]

    # --- orchestration ---------------------------------------------------------
    def prepare(self, app: Application) -> Application:
        """Generate everything an application needs and write it to disk."""
        app.cover_letter = self.cover_letter(app.job)
        app.tailored_resume = self.tailored_resume(app.job)
        app.answers = dict(self.profile.answers or {})
        app.resume_file = self.profile.resume_path
        self._write_bundle(app)
        return app

    def _write_bundle(self, app: Application) -> Path:
        """Save the generated documents next to each other for review."""
        slug = _slug(f"{app.job.company}-{app.job.title}")
        out_dir = self.config.output_dir / f"{slug}-{app.fingerprint[:8]}"
        out_dir.mkdir(parents=True, exist_ok=True)

        (out_dir / "cover-letter.md").write_text(app.cover_letter or "", encoding="utf-8")
        if app.tailored_resume:
            (out_dir / "resume-slant.md").write_text(app.tailored_resume, encoding="utf-8")
        (out_dir / "job.md").write_text(
            f"# {app.job.title}\n\n**{app.job.company}** - {app.job.location or 'Remote'}\n\n"
            f"Source: {app.job.source} | Apply: {app.job.apply_method}\n\n"
            f"{app.job.url}\n\nScore: {app.score}\n\n"
            + "\n".join(f"- {r}" for r in app.match_reasons)
            + f"\n\n---\n\n{app.job.description or ''}",
            encoding="utf-8",
        )
        return out_dir


def _match_known_answer(question: str, known: dict[str, str]) -> str:
    """Map a form question onto a configured answer key by intent."""
    q = question.lower()
    intents = [
        (("notice period", "when can you start", "availability", "start date"), "notice_period"),
        (("sponsor", "visa", "right to work", "work authorisation", "work authorization",
          "legally authorized", "eligible to work"), "work_authorization"),
        (("salary", "compensation expectation", "expected ctc", "desired pay"),
         "salary_expectation"),
        (("relocate", "relocation"), "relocation"),
        (("years of experience", "how many years"), "years_experience"),
        (("linkedin",), "linkedin_url"),
        (("github",), "github_url"),
        (("portfolio", "website", "personal site"), "portfolio_url"),
        (("why do you want", "why are you interested"), "why_this_company"),
        (("pronoun",), "pronouns"),
        (("gender", "ethnicity", "race", "veteran", "disability"), "eeo_decline"),
        (("hybrid", "onsite", "office", "days per week"), "work_preference"),
        (("current company", "current employer"), "current_company"),
        (("current location", "where are you based"), "location"),
    ]
    for phrases, key in intents:
        if any(p in q for p in phrases) and known.get(key):
            return str(known[key])
    # Direct key mention, e.g. a config key of "security_clearance".
    for key, value in known.items():
        if key.replace("_", " ") in q and value:
            return str(value)
    return ""


def _clean_letter(text: str) -> str:
    """Strip the preamble models sometimes add, and normalise dashes."""
    text = re.sub(r"^\s*(here('s| is)[^\n]*|cover letter:?)\s*\n+", "", text, flags=re.I)
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text.strip())
    return text.replace("—", " - ").replace("–", "-").strip()


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "job"
