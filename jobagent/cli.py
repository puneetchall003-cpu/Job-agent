"""Command line interface.

    jobagent init            scaffold config.yaml
    jobagent profile         show what was parsed out of your resume
    jobagent discover        find jobs, score them, apply to nothing
    jobagent run             the daily job: discover, tailor, apply
    jobagent apply --queue   work the browser-assisted queue
    jobagent status          what has happened so far
    jobagent digest          write/send the HTML digest
    jobagent linkedin        print your prefiltered LinkedIn search links
    jobagent verify-boards   check which configured ATS boards are live
    jobagent login-browser   log into LinkedIn once in the agent's browser profile
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .config import load_config
from .db import Store
from .models import STATUS_QUEUED, STATUS_TAILORED

log = logging.getLogger("jobagent")

EXAMPLE_CONFIG = Path(__file__).parent / "config.example.yaml"


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --- commands ------------------------------------------------------------------
def cmd_init(args) -> int:
    target = Path(args.config)
    if target.exists() and not args.force:
        print(f"{target} already exists. Use --force to overwrite.")
        return 1
    shutil.copy(EXAMPLE_CONFIG, target)
    print(f"Wrote {target}\n\nNext:")
    print("  1. Put your resume somewhere and set profile.resume_path")
    print("  2. Fill in profile.answers (notice period, visa, salary)")
    print("  3. export ANTHROPIC_API_KEY=...   (optional but worth it)")
    print("  4. jobagent profile     # check what it parsed")
    print("  5. jobagent discover    # see what it finds, applies to nothing")
    return 0


def cmd_profile(args) -> int:
    from .llm import build_llm
    from .resume import build_profile

    config = load_config(args.config)
    profile = build_profile(config, build_llm(config))

    print(f"\n  Name        {profile.full_name or '(not found)'}")
    print(f"  Email       {profile.email or '(not found)'}")
    print(f"  Phone       {profile.phone or '(not found)'}")
    print(f"  Location    {profile.location or '(not found)'}")
    print(f"  Headline    {profile.headline or '(not found)'}")
    print(f"  Experience  {profile.years_experience or '?'} years")
    print(f"  LinkedIn    {profile.linkedin_url or '-'}")
    print(f"  GitHub      {profile.github_url or '-'}")
    print(f"  Resume      {profile.resume_path or '(none)'} "
          f"({len(profile.resume_text)} chars extracted)")
    print(f"\n  Skills ({len(profile.skills)}): {', '.join(profile.skills[:30])}")
    if profile.certifications:
        print(f"  Certs: {', '.join(profile.certifications)}")
    if profile.titles:
        print(f"  Titles: {', '.join(profile.titles[:6])}")
    print(f"\n  Form answers ({len(profile.answers)}):")
    for key, value in profile.answers.items():
        print(f"    {key:24} {str(value)[:60]}")
    if not profile.answers:
        print("    none - fill in profile.answers or every form will need you")
    missing = [f for f in ("full_name", "email", "phone") if not getattr(profile, f)]
    if missing:
        print(f"\n  Missing required field(s): {', '.join(missing)}. "
              f"Set them under `profile:` in config.yaml.")
        return 1
    return 0


def cmd_discover(args) -> int:
    """Find and score jobs. Never submits anything."""
    from .pipeline import Agent

    config = load_config(args.config)
    agent = Agent(config)
    try:
        agent._report = _empty_report()
        jobs = agent.discover()
        agent._report.discovered = len(jobs)
        matched = agent.evaluate(jobs)

        print("\n  Sources: " + ", ".join(
            f"{name}={count}" for name, count in agent._report.per_source.items()))
        print(f"  {len(jobs)} unique jobs, {agent._report.new} new, "
              f"{len(matched)} above threshold\n")
        for app in matched[: args.limit]:
            job = app.job
            print(f"  [{app.score:5.1f}] {job.title}")
            print(f"          {job.company} - {job.location or 'Remote'} "
                  f"({job.country or '?'}) via {job.source} / {job.apply_method}")
            for reason in app.match_reasons[:2]:
                print(f"          - {reason}")
            print(f"          {job.url}")
        if not matched:
            print("  Nothing cleared the bar. Lower match.min_score or widen search.titles.")
    finally:
        agent.close()
    return 0


def cmd_run(args) -> int:
    """The daily job."""
    from .digest import send_digest, write_digest
    from .pipeline import Agent

    config = load_config(args.config)
    if args.apply:
        config.apply.enabled = True
    dry_run = not args.apply

    agent = Agent(config)
    try:
        confirm = _confirm_prompt if (config.apply.require_confirmation and args.apply) else None
        report = agent.run(dry_run=dry_run, limit=args.limit, confirm=confirm)

        print(f"\n  {report.summary()}")
        if report.errors:
            print("\n  Errors:")
            for error in report.errors[:8]:
                print(f"    - {error}")

        path = write_digest(config, agent.store, report)
        print(f"\n  Digest: {path}")
        if send_digest(config, agent.store, report):
            print(f"  Emailed to {config.notify.to_addr}")
        if dry_run:
            print("\n  This was a dry run - nothing was submitted.")
            print("  Add --apply once the matches look right.")
    finally:
        agent.close()
    return 0


def cmd_apply(args) -> int:
    """Work the queue: open each prefilled application for you to submit."""
    from .pipeline import Agent

    config = load_config(args.config)
    config.apply.enabled = True
    agent = Agent(config)
    try:
        confirm = _confirm_prompt if config.apply.require_confirmation else None
        report = agent.process_queue(dry_run=args.dry_run, limit=args.limit, confirm=confirm)
        print(f"\n  {report.summary()}")
    finally:
        agent.close()
    return 0


def cmd_status(args) -> int:
    config = load_config(args.config)
    with Store(config.db_path) as store:
        stats = store.stats()
        total = sum(stats.values())
        print(f"\n  {total} jobs tracked")
        for status, count in sorted(stats.items(), key=lambda kv: -kv[1]):
            print(f"    {status:10} {count}")
        print(f"\n  Applied today: {store.applied_today()} "
              f"/ {config.apply.daily_limit}")

        queued = store.by_status(STATUS_QUEUED, STATUS_TAILORED, limit=args.limit)
        if queued:
            print(f"\n  Waiting for you ({len(queued)}):")
            for app in queued:
                print(f"    [{app.score:5.1f}] {app.job.title} @ {app.job.company} "
                      f"({app.job.apply_method})")
                if app.error:
                    print(f"            {app.error[:90]}")

        recent = [a for a in store.recent(args.limit) if a.applied_at]
        if recent:
            print("\n  Recently applied:")
            for app in recent:
                print(f"    {app.applied_at[:10]}  {app.job.title} @ {app.job.company}")
    return 0


def cmd_digest(args) -> int:
    from .digest import send_digest, write_digest

    config = load_config(args.config)
    with Store(config.db_path) as store:
        path = write_digest(config, store)
        print(f"  Wrote {path}")
        if args.send and send_digest(config, store):
            print(f"  Emailed to {config.notify.to_addr}")
        if args.open:
            webbrowser.open(path.resolve().as_uri())
    return 0


def cmd_linkedin(args) -> int:
    from .sources.linkedin import search_urls

    config = load_config(args.config)
    searches = search_urls(config)
    print(f"\n  {len(searches)} prefiltered LinkedIn searches:\n")
    for search in searches:
        print(f"  {search['country']}  {search['title']}")
        print(f"      {search['url']}\n")
    if args.open:
        for search in searches[:6]:
            webbrowser.open(search["url"])
    return 0


def cmd_verify_boards(args) -> int:
    """Check every configured ATS board token and report which are live."""
    from .sources import HttpClient
    from .sources.ats import AshbySource, GreenhouseSource, LeverSource

    config = load_config(args.config)
    http = HttpClient(delay=0.3)
    live, dead = [], []

    for cls in (GreenhouseSource, LeverSource, AshbySource):
        source = cls(config, http)
        for token in source.companies():
            jobs = list(source.fetch_company(token))
            entry = f"{source.name}:{token}"
            if jobs:
                live.append((entry, len(jobs)))
                print(f"  ok    {entry:34} {len(jobs)} open roles")
            else:
                dead.append(entry)
                print(f"  DEAD  {entry:34} no jobs / bad token")

    print(f"\n  {len(live)} live, {len(dead)} dead")
    if dead:
        print("  Remove the dead ones from config.yaml:")
        for entry in dead:
            print(f"    - {entry.split(':', 1)[1]}   ({entry.split(':')[0]})")
    return 0


def cmd_login_browser(args) -> int:
    """Open the agent's persistent Chromium profile so you can log in once."""
    config = load_config(args.config)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Needs playwright: pip install playwright && playwright install chromium")
        return 1

    profile_dir = config.apply.browser_profile_dir
    print(f"\n  Opening Chromium with profile {profile_dir}")
    print("  Log into LinkedIn (and any job sites you use), then close the window.")
    print("  The session is saved locally and reused for assisted applying.\n")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            profile_dir, headless=False, viewport={"width": 1440, "height": 900})
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.linkedin.com/login")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    print("  Session saved.")
    return 0


# --- helpers -------------------------------------------------------------------
def _confirm_prompt(app) -> bool:
    job = app.job
    print(f"\n  [{app.score:5.1f}] {job.title}")
    print(f"          {job.company} - {job.location or 'Remote'} via {job.apply_method}")
    print(f"          {job.url}")
    for reason in app.match_reasons[:3]:
        print(f"          - {reason}")
    return input("  Apply to this? [y/N] ").strip().lower() == "y"


def _empty_report():
    from .pipeline import RunReport
    return RunReport()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobagent",
        description="Find, tailor and submit job applications across LinkedIn, "
                    "company ATS boards and job aggregators.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"jobagent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter config.yaml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("profile", help="show the parsed resume profile")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("discover", help="find and score jobs, submit nothing")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("run", help="the daily job: discover, tailor, apply")
    p.add_argument("--apply", action="store_true",
                   help="actually submit (without this it is a dry run)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap how many matches to process this run")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("apply", help="work the queue in your browser")
    p.add_argument("--queue", action="store_true", help="(default behaviour)")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("status", help="what the agent has done")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("digest", help="write or email the HTML digest")
    p.add_argument("--send", action="store_true")
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("linkedin", help="print prefiltered LinkedIn search links")
    p.add_argument("--open", action="store_true", help="open them in your browser")
    p.set_defaults(func=cmd_linkedin)

    p = sub.add_parser("verify-boards", help="check which ATS board tokens are live")
    p.set_defaults(func=cmd_verify_boards)

    p = sub.add_parser("login-browser", help="log in once for assisted applying")
    p.set_defaults(func=cmd_login_browser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"\n  {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
