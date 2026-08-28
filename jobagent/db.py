"""SQLite persistence.

One row per job fingerprint. The DB is the memory that stops the agent
re-applying to a role it already handled, across runs and across sources.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import (
    Application,
    Job,
    STATUS_APPLIED,
    STATUS_NEW,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    fingerprint   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT,
    country       TEXT,
    url           TEXT,
    apply_method  TEXT,
    status        TEXT NOT NULL,
    score         REAL DEFAULT 0,
    match_reasons TEXT,
    cover_letter  TEXT,
    tailored_resume TEXT,
    resume_file   TEXT,
    answers       TEXT,
    error         TEXT,
    job_json      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    applied_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applied_at ON applications(applied_at);
CREATE INDEX IF NOT EXISTS idx_company ON applications(company);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    discovered INTEGER DEFAULT 0,
    matched    INTEGER DEFAULT 0,
    applied    INTEGER DEFAULT 0,
    notes      TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- reads -----------------------------------------------------------------
    def known(self, fingerprint: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM applications WHERE fingerprint = ?", (fingerprint,)
        )
        return cur.fetchone() is not None

    def get(self, fingerprint: str) -> Optional[Application]:
        row = self._conn.execute(
            "SELECT * FROM applications WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return _row_to_app(row) if row else None

    def by_status(self, *statuses: str, limit: int = 200) -> list[Application]:
        marks = ",".join("?" * len(statuses))
        rows = self._conn.execute(
            f"SELECT * FROM applications WHERE status IN ({marks}) "
            f"ORDER BY score DESC, updated_at DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
        return [_row_to_app(r) for r in rows]

    def recent(self, limit: int = 50) -> list[Application]:
        rows = self._conn.execute(
            "SELECT * FROM applications ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_app(r) for r in rows]

    def applied_since(self, since: datetime) -> list[Application]:
        rows = self._conn.execute(
            "SELECT * FROM applications WHERE status = ? AND applied_at >= ? "
            "ORDER BY applied_at DESC",
            (STATUS_APPLIED, since.isoformat()),
        ).fetchall()
        return [_row_to_app(r) for r in rows]

    def applied_today(self) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return len(self.applied_since(start))

    def applied_to_company_recently(self, company: str, days: int = 14) -> bool:
        """Guard against carpet-bombing one company across several of its openings."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM applications WHERE lower(company) = lower(?) "
            "AND status = ? AND applied_at >= ? LIMIT 1",
            (company, STATUS_APPLIED, since),
        ).fetchone()
        return row is not None

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) c FROM applications GROUP BY status"
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    # --- writes ----------------------------------------------------------------
    def upsert_job(self, job: Job) -> Application:
        """Insert a freshly discovered job, or return the existing record untouched."""
        existing = self.get(job.fingerprint)
        if existing:
            return existing
        app = Application(fingerprint=job.fingerprint, job=job, status=STATUS_NEW)
        self.save(app)
        return app

    def save(self, app: Application) -> None:
        app.touch(app.status)
        self._conn.execute(
            """
            INSERT INTO applications (
                fingerprint, source, title, company, location, country, url,
                apply_method, status, score, match_reasons, cover_letter,
                tailored_resume, resume_file, answers, error, job_json,
                created_at, updated_at, applied_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                status=excluded.status, score=excluded.score,
                match_reasons=excluded.match_reasons,
                cover_letter=excluded.cover_letter,
                tailored_resume=excluded.tailored_resume,
                resume_file=excluded.resume_file, answers=excluded.answers,
                error=excluded.error, job_json=excluded.job_json,
                updated_at=excluded.updated_at, applied_at=excluded.applied_at
            """,
            (
                app.fingerprint, app.job.source, app.job.title, app.job.company,
                app.job.location, app.job.country, app.job.url, app.job.apply_method,
                app.status, app.score, json.dumps(app.match_reasons), app.cover_letter,
                app.tailored_resume, app.resume_file, json.dumps(app.answers),
                app.error, json.dumps(app.job.to_dict()), app.created_at,
                app.updated_at, app.applied_at,
            ),
        )
        self._conn.commit()

    def start_run(self) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, discovered: int, matched: int, applied: int,
                   notes: str = "") -> None:
        self._conn.execute(
            "UPDATE runs SET ended_at=?, discovered=?, matched=?, applied=?, notes=? "
            "WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), discovered, matched, applied,
             notes, run_id),
        )
        self._conn.commit()


def _row_to_app(row: sqlite3.Row) -> Application:
    job_data = json.loads(row["job_json"])
    job_data.pop("fingerprint", None)
    job = Job(**job_data)
    return Application(
        fingerprint=row["fingerprint"],
        job=job,
        status=row["status"],
        score=row["score"] or 0.0,
        match_reasons=json.loads(row["match_reasons"] or "[]"),
        cover_letter=row["cover_letter"] or "",
        tailored_resume=row["tailored_resume"] or "",
        resume_file=row["resume_file"] or "",
        answers=json.loads(row["answers"] or "{}"),
        error=row["error"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        applied_at=row["applied_at"],
    )
