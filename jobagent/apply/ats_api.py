"""Direct submission through ATS application APIs.

These are the only paths where the agent submits without a browser. Both
vendors gate their write endpoints differently per customer, so a driver that
gets rejected returns `needs_human` and the pipeline hands the job to the
assisted flow rather than silently dropping it.
"""
from __future__ import annotations

import logging
from pathlib import Path

import requests

from ..models import Application, Profile
from .base import ApplyResult, BaseDriver

log = logging.getLogger(__name__)
TIMEOUT = 60


class GreenhouseDriver(BaseDriver):
    """POST to the Greenhouse Job Board application endpoint.

    Some boards accept an unauthenticated post from their embedded form; others
    require a Job Board API key. Set one per company under
    `apply.greenhouse_keys: {board_token: key}` if you have it.
    """

    method = "greenhouse"
    ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"

    def submit(self, app: Application, profile: Profile, dry_run: bool = True) -> ApplyResult:
        meta = app.job.apply_meta or {}
        token, job_id = meta.get("board_token"), meta.get("job_id")
        if not token or not job_id:
            return ApplyResult.human("missing greenhouse board token or job id")

        first, last = self.split_name(profile)
        if not first or not profile.email:
            return ApplyResult.failure("profile is missing a name or email")

        resume = self.resume_path(app, profile)
        if not resume:
            return ApplyResult.human("no resume file on disk to attach")

        data = {
            "first_name": first,
            "last_name": last,
            "email": profile.email,
            "phone": profile.phone or "",
            "cover_letter_text": app.cover_letter or "",
        }
        for key, url in (("linkedin_url", profile.linkedin_url),
                         ("website", profile.portfolio_url or profile.github_url)):
            if url:
                data[key] = url

        url = self.ENDPOINT.format(token=token, job_id=job_id)
        if dry_run:
            return ApplyResult.success(
                f"DRY RUN - would POST to {url}",
                details={"fields": sorted(data), "resume": resume},
            )

        api_key = (self.config.raw.get("apply", {}).get("greenhouse_keys", {}) or {}).get(token)
        auth = (api_key, "") if api_key else None
        try:
            with open(resume, "rb") as fh:
                response = requests.post(
                    url, data=data, files={"resume": (Path(resume).name, fh)},
                    auth=auth, timeout=TIMEOUT,
                )
        except requests.RequestException as exc:
            return ApplyResult.failure(f"network error: {exc}")

        if response.status_code in (200, 201):
            body = _json(response)
            return ApplyResult.success(
                "submitted via Greenhouse API",
                confirmation=str(body.get("success") or body.get("id") or response.status_code),
            )
        if response.status_code in (401, 403):
            return ApplyResult.human(
                f"Greenhouse board {token} requires an API key for direct submission "
                f"(HTTP {response.status_code}) - falling back to the browser flow"
            )
        return ApplyResult.human(
            f"Greenhouse rejected the submission (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )


class LeverDriver(BaseDriver):
    """POST to Lever's postings apply endpoint."""

    method = "lever"
    ENDPOINT = "https://api.lever.co/v0/postings/{site}/{posting_id}"

    def submit(self, app: Application, profile: Profile, dry_run: bool = True) -> ApplyResult:
        meta = app.job.apply_meta or {}
        site, posting_id = meta.get("site"), meta.get("posting_id")
        if not site or not posting_id:
            return ApplyResult.human("missing lever site or posting id")
        if not profile.full_name or not profile.email:
            return ApplyResult.failure("profile is missing a name or email")

        resume = self.resume_path(app, profile)
        if not resume:
            return ApplyResult.human("no resume file on disk to attach")

        data = {
            "name": profile.full_name,
            "email": profile.email,
            "phone": profile.phone or "",
            "comments": app.cover_letter or "",
        }
        if profile.linkedin_url:
            data["urls[LinkedIn]"] = profile.linkedin_url
        if profile.github_url:
            data["urls[GitHub]"] = profile.github_url

        url = self.ENDPOINT.format(site=site, posting_id=posting_id)
        if dry_run:
            return ApplyResult.success(
                f"DRY RUN - would POST to {url}?send=true",
                details={"fields": sorted(data), "resume": resume},
            )

        try:
            with open(resume, "rb") as fh:
                response = requests.post(
                    url, params={"send": "true"}, data=data,
                    files={"resume": (Path(resume).name, fh)}, timeout=TIMEOUT,
                )
        except requests.RequestException as exc:
            return ApplyResult.failure(f"network error: {exc}")

        if response.status_code in (200, 201):
            body = _json(response)
            return ApplyResult.success(
                "submitted via Lever API",
                confirmation=str(body.get("applicationId") or body.get("ok") or response.status_code),
            )
        return ApplyResult.human(
            f"Lever rejected the submission (HTTP {response.status_code}): {response.text[:300]}"
        )


def _json(response) -> dict:
    try:
        body = response.json()
        return body if isinstance(body, dict) else {"result": body}
    except ValueError:
        return {}
