"""Daily digest: an HTML summary of the run, optionally emailed to you."""
from __future__ import annotations

import html
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from .config import Config
from .db import Store
from .models import Application, STATUS_QUEUED, STATUS_TAILORED
from .sources.linkedin import search_urls

log = logging.getLogger(__name__)

STYLE = """
body{font:15px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif;color:#0f172a;
 background:#f8fafc;margin:0;padding:24px}
.wrap{max-width:760px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;
 border-radius:12px;padding:28px}
h1{font-size:21px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px;
 text-transform:uppercase;letter-spacing:.06em;color:#64748b}
.meta{color:#64748b;font-size:13px;margin-bottom:18px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 4px}
.stat{background:#f1f5f9;border-radius:8px;padding:8px 14px;font-size:13px}
.stat b{display:block;font-size:19px;color:#0f172a}
.job{border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;margin-bottom:10px}
.job a{color:#0369a1;text-decoration:none;font-weight:600;font-size:15px}
.sub{color:#475569;font-size:13px;margin:2px 0 8px}
.score{float:right;background:#0f172a;color:#fff;border-radius:6px;
 padding:2px 9px;font-size:12px;font-weight:700}
.reasons{color:#475569;font-size:13px;margin:0;padding-left:18px}
.tag{display:inline-block;background:#e0f2fe;color:#075985;border-radius:5px;
 padding:1px 7px;font-size:11px;margin-right:5px;text-transform:uppercase;
 letter-spacing:.04em}
.tag.auto{background:#dcfce7;color:#166534}.tag.manual{background:#fef3c7;color:#92400e}
.links a{display:block;padding:7px 0;color:#0369a1;font-size:14px}
.empty{color:#64748b;font-style:italic}
"""


def build_html(config: Config, store: Store, report=None) -> str:
    queued = store.by_status(STATUS_QUEUED, STATUS_TAILORED, limit=40)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    applied = store.applied_since(since)
    stats = store.stats()
    now = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<style>{STYLE}</style><div class='wrap'>",
        "<h1>Job agent digest</h1>",
        f"<div class='meta'>{now}</div>",
        "<div class='stats'>",
    ]
    tiles = [
        ("Applied (24h)", len(applied)),
        ("Awaiting you", len(queued)),
        ("Applied total", stats.get("applied", 0)),
        ("Screened out", stats.get("skipped", 0)),
    ]
    if report is not None:
        tiles.insert(0, ("Found this run", report.discovered))
    for label, value in tiles:
        parts.append(f"<div class='stat'><b>{value}</b>{html.escape(label)}</div>")
    parts.append("</div>")

    parts.append("<h2>Applied in the last 24 hours</h2>")
    parts.append(_jobs_html(applied) if applied
                 else "<p class='empty'>Nothing submitted yet.</p>")

    parts.append("<h2>Ready for you to submit</h2>")
    if queued:
        parts.append(
            "<p class='meta'>Run <code>jobagent apply --queue</code> and the agent "
            "opens each of these prefilled in your browser.</p>")
        parts.append(_jobs_html(queued))
    else:
        parts.append("<p class='empty'>Queue is clear.</p>")

    parts.append("<h2>Your LinkedIn searches</h2>")
    parts.append("<div class='links'>")
    for search in search_urls(config):
        label = f"{search['title']} - {search['country']}"
        parts.append(f"<a href='{html.escape(search['url'])}'>{html.escape(label)}</a>")
    parts.append("</div>")

    if report is not None and report.errors:
        parts.append("<h2>Errors</h2><ul class='reasons'>")
        for error in report.errors[:12]:
            parts.append(f"<li>{html.escape(error)}</li>")
        parts.append("</ul>")

    parts.append("</div>")
    return "".join(parts)


def _jobs_html(apps: list[Application]) -> str:
    rows = []
    for app in apps:
        job = app.job
        auto = job.apply_method in ("greenhouse", "lever")
        tag_class = "auto" if auto else "manual"
        tag_text = "auto-apply" if auto else "needs you"
        location = job.location or ("Remote" if job.remote else "-")
        reasons = "".join(
            f"<li>{html.escape(r)}</li>" for r in (app.match_reasons or [])[:3])
        rows.append(
            f"<div class='job'><span class='score'>{app.score:.0f}</span>"
            f"<a href='{html.escape(job.url)}'>{html.escape(job.title)}</a>"
            f"<div class='sub'>{html.escape(job.company)} - {html.escape(location)}"
            f" - {html.escape(job.country or 'n/a')}</div>"
            f"<span class='tag {tag_class}'>{tag_text}</span>"
            f"<span class='tag'>{html.escape(job.source)}</span>"
            f"<ul class='reasons'>{reasons}</ul></div>"
        )
    return "".join(rows)


def write_digest(config: Config, store: Store, report=None) -> Path:
    path = Path(config.data_dir) / "digest.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_html(config, store, report), encoding="utf-8")
    return path


def send_digest(config: Config, store: Store, report=None) -> bool:
    """Email the digest. Returns False (with a log line) rather than raising."""
    notify = config.notify
    if not notify.enabled:
        return False
    missing = [f for f in ("smtp_host", "from_addr", "to_addr")
               if not getattr(notify, f)]
    if missing:
        log.warning("notify enabled but missing: %s", ", ".join(missing))
        return False

    message = EmailMessage()
    message["Subject"] = _subject(store, report)
    message["From"] = notify.from_addr
    message["To"] = notify.to_addr
    message.set_content("Your job agent digest is in HTML. Open it in an HTML-capable client.")
    message.add_alternative(build_html(config, store, report), subtype="html")

    try:
        with smtplib.SMTP(notify.smtp_host, notify.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if notify.smtp_user:
                smtp.login(notify.smtp_user, notify.smtp_password)
            smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 - a failed email must not fail the run
        log.error("Could not send digest: %s", exc)
        return False
    log.info("Digest emailed to %s", notify.to_addr)
    return True


def _subject(store: Store, report=None) -> str:
    queued = len(store.by_status(STATUS_QUEUED, STATUS_TAILORED, limit=200))
    applied = len(store.applied_since(datetime.now(timezone.utc) - timedelta(days=1)))
    if report is not None:
        return f"Job agent: {applied} applied, {queued} ready, {report.discovered} found"
    return f"Job agent: {applied} applied, {queued} ready for you"
