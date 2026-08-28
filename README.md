# job-agent

An agent that finds Lead DevOps / SRE / Platform roles across **India, the UK and
the USA**, scores them against your resume, writes a tailored cover letter for
each one, and applies — automatically where it can, and with one click from you
where it can't.

```
jobagent run --apply
```

---

## Read this first: how it handles LinkedIn

LinkedIn's User Agreement prohibits bots, scraping and automated applying, and
they enforce it with account restrictions. Your LinkedIn account is the most
valuable thing in your job search, so this agent will not quietly gamble it.

**What it does instead:**

| Path | How it applies | Risk |
|---|---|---|
| Greenhouse, Lever boards | Fully automatic via official ATS APIs | None — public, documented APIs |
| Everything else, LinkedIn included | Opens the form in *your* browser, fills every field it can, you press Submit | None — a human sends it |
| LinkedIn search links | Prefiltered deep links in your digest | None — just URLs |
| LinkedIn scraping | Off by default, opt-in behind two flags | Your account, your call |

You lose very little by never scraping LinkedIn: most roles there are syndicated
from an ATS, so the Greenhouse/Lever/Adzuna sources find the same jobs — with an
application path the agent can actually drive end to end.

The assisted path still means near-zero effort per application. The agent has
already found the role, decided it's worth applying to, written the letter and
filled the form. You read it and click.

---

## Setup

```bash
git clone <this repo> && cd Job-agent
pip install -e ".[all]"
playwright install chromium        # for browser-assisted applying

jobagent init                      # writes config.yaml
```

Then edit `config.yaml`:

1. **`profile.resume_path`** — point at your resume (`.pdf`, `.docx`, `.txt`, `.md`).
2. **`profile.answers`** — notice period, visa status, salary expectations.
   The agent uses these *verbatim* and never guesses. Anything you leave blank
   becomes a field it hands back to you, so filling these in properly is what
   turns a half-filled form into a one-click submit.
3. **`search.titles`** — already seeded with Lead DevOps / SRE / Platform titles.

Optional keys, in your environment (never in the config file):

```bash
export ANTHROPIC_API_KEY=...   # AI screening + tailored cover letters
export ADZUNA_APP_ID=...       # free at developer.adzuna.com - the main IN/UK/US feed
export ADZUNA_APP_KEY=...
```

Without `ANTHROPIC_API_KEY` everything still runs — discovery, keyword matching,
the digest, template cover letters — you just lose the AI screening and the
tailored writing. Without the Adzuna key you lose most of the job volume.

Check the parse before you trust it:

```bash
jobagent profile
```

---

## Daily use

```bash
jobagent discover        # find and rank jobs, submit nothing
jobagent run             # dry run: discover, score, tailor, queue
jobagent run --apply     # the real thing, asks before each submission
jobagent apply --queue   # work the browser queue: form opens prefilled, you submit
jobagent status          # what's applied, what's waiting
jobagent digest --open   # the HTML summary
```

A sensible first week:

```bash
jobagent run                     # look at what it picked. Adjust min_score/titles.
jobagent run --apply             # let it apply, confirming each one
jobagent apply --queue           # clear the browser-assisted backlog
```

For assisted applying, log in once so the agent has a session to reuse:

```bash
jobagent login-browser           # log into LinkedIn etc, then close the window
```

---

## How it decides

Two stages, cheap before expensive.

**1. Deterministic scoring** (free, every job). Hard blockers first — wrong
country, junior title, excluded company, stale posting, salary floor. Then a
weighted score out of 100:

| Signal | Weight |
|---|---|
| Skill overlap with your resume (high-value skills like Kubernetes/Terraform count double) | 45 |
| Title alignment with your target titles | 25 |
| Seniority markers (lead, principal, staff, head) | 15 |
| Your configured keywords | 10 |
| Salary against your floor (unknown is neutral, never a penalty) | 5 |

**2. AI screening** (top 25 only, cost-controlled). Claude reads the actual
description and gives a blunt verdict — catching what keywords can't, in both
directions: the "DevOps Engineer" role that's really a Java backend job, and the
oddly-titled role that's a perfect fit. The verdict is blended 65/35 with the
keyword score, so one strange call can't dominate. If it says *don't apply*, the
job is dropped with its reason recorded.

Everything the agent decides is written to SQLite, so it never reconsiders a job
it already rejected and never applies to the same role twice — even when three
different sources list it. Deduplication is on company + title + location, not
URL, precisely because the same role appears under three different links.

---

## What it writes for each application

Under `data/applications/<company>-<role>-<id>/`:

- `cover-letter.md` — 200-280 words, grounded strictly in your resume. The prompt
  forbids inventing employers, metrics or technologies.
- `resume-slant.md` — your summary, skills and top 5 bullets reordered to lead
  with what this specific job asks for. Reordered, never fabricated.
- `job.md` — the posting, the score, and why it matched.

Review any of these before submitting. They're plain markdown.

---

## Safety

The agent applies for jobs under your name. The defaults reflect that.

- **`apply.enabled: false`** — the master switch. Nothing submits until you pass
  `--apply`.
- **`require_confirmation: true`** — it asks before every single submission.
- **`daily_limit: 15`** — a bug can't fire off 400 applications overnight.
- **One company per 14 days** — no carpet-bombing every opening at a company.
- **Sensitive questions never reach the model.** Visa status, notice period and
  salary come from your config verbatim, or the field is left for you. The agent
  will not guess your immigration status.
- **A refused API submission falls back to the browser**, it isn't silently dropped.
- **Errors are contained per-source and per-job** — one dead board or one crashed
  driver never takes down the run.

---

## Where the jobs come from

| Source | Coverage | Apply method |
|---|---|---|
| **Adzuna** | India, UK, USA — the main volume | assisted |
| **Greenhouse** | ~28 company boards, incl. Monzo, Wise, Razorpay, GitLab | **automatic** |
| **Lever** | Netflix, Revolut, Spotify, Plaid… | **automatic** |
| **Ashby** | Linear, Vanta, Deel, Notion… | assisted |
| **Remotive / RemoteOK / Arbeitnow / Jobicy** | Remote roles | assisted |
| **LinkedIn** | Search links always; scraping opt-in | assisted |

Add any company you care about — the board token is the last path segment of
their careers URL (`job-boards.greenhouse.io/TOKEN`, `jobs.lever.co/TOKEN`).

The shipped company list was assembled by hand and not all tokens are guaranteed
live. Prune it in one command:

```bash
jobagent verify-boards
```

It reports which boards responded with open roles and prints the dead ones for
you to delete.

---

## Scheduling

`.github/workflows/daily.yml` runs discovery every weekday morning and emails you
the digest. It deliberately does **not** submit — CI has no browser you can watch
and no way for you to confirm. It queues; you apply locally.

Locally, cron works fine:

```cron
0 9 * * 1-5  cd /path/to/Job-agent && jobagent run && jobagent digest --send
```

---

## Layout

```
jobagent/
  cli.py          commands
  config.py       config.yaml + ${ENV} expansion
  models.py       Job, Profile, Application, fingerprinting
  db.py           SQLite: dedupe, history, daily limits
  resume.py       PDF/DOCX/TXT -> structured profile
  matching.py     blockers, weighted scoring, AI screening
  llm.py          Claude client (retry, call budget, null fallback)
  tailor.py       cover letters, resume slants, form answers
  digest.py       HTML digest + email
  pipeline.py     discover -> score -> tailor -> apply
  sources/        adzuna, greenhouse, lever, ashby, remotive, ... , linkedin
  apply/          greenhouse API, lever API, browser-assisted
```

Tests: `python -m pytest` (91 tests, no network required).

---

## Honest limitations

- **The ATS submit endpoints vary per company.** Greenhouse and Lever gate their
  write APIs differently per customer. When one refuses, the agent routes that
  job to the browser flow automatically — but the *first* auto-submission to any
  new board is worth watching. Use `--apply` with confirmation on until you've
  seen it succeed.
- **Workday and Taleo have no usable API.** They go through the browser flow,
  where their multi-step wizards mean the agent fills page one and you finish it.
- **Country inference is heuristic**, based on city and state names. A posting
  listed only as "EMEA" gets screened out rather than guessed at.
- **The AI screen costs money.** Roughly 25 calls per run at `llm_top_n: 25`.
  Set `use_llm: false` to run purely on keywords.

## License

MIT.
