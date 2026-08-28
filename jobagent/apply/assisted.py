"""Browser-assisted applying.

Opens the real application page in your own logged-in Chromium profile, fills
in everything it can recognise, attaches the resume, pastes the tailored cover
letter, answers the questions it has answers for, then stops and highlights
what is left. You read it and press Submit.

This is the universal path: it works on LinkedIn Easy Apply, Workday, Greenhouse
and Lever embedded forms, and anything else with a normal HTML form. It is also
the honest one - a human reviews and sends every application, which is what
LinkedIn's terms require and what stops a bad autofill going out 40 times.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..models import Application, Profile
from .base import ApplyResult, BaseDriver

log = logging.getLogger(__name__)

# Field-matching rules, most specific first. Each entry maps a regex over the
# field's label/name/placeholder to a profile attribute.
FIELD_RULES: list[tuple[str, str]] = [
    (r"first[\s_-]*name|given[\s_-]*name|fname", "first_name"),
    (r"last[\s_-]*name|surname|family[\s_-]*name|lname", "last_name"),
    (r"full[\s_-]*name|^name$|your name", "full_name"),
    (r"e[\s_-]*mail", "email"),
    (r"phone|mobile|contact number|telephone", "phone"),
    (r"linked\s*in", "linkedin_url"),
    (r"git\s*hub", "github_url"),
    (r"portfolio|personal (?:web)?site|website|blog", "portfolio_url"),
    (r"current (?:city|location)|city|location|where are you based", "location"),
    (r"cover letter|why (?:do you )?(?:want|are you)|message to", "cover_letter"),
]

SKIP_FIELD = re.compile(
    r"password|captcha|search|honeypot|confirm your email|referral code", re.I)


class AssistedDriver(BaseDriver):
    """Prefill an application form and hand control back to the human."""

    method = "assisted"

    def submit(self, app: Application, profile: Profile, dry_run: bool = True) -> ApplyResult:
        if dry_run:
            return ApplyResult.success(
                f"DRY RUN - would open {app.job.url} and prefill the form"
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return ApplyResult.failure(
                "assisted mode needs playwright:\n"
                "  pip install playwright && playwright install chromium"
            )

        values = self._values(app, profile)
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                self.config.apply.browser_profile_dir,
                headless=False,  # you must be able to see and submit it
                viewport={"width": 1440, "height": 960},
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(app.job.url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                self._open_application_form(page)
                filled, unfilled = self._fill(page, values, app, profile)
                self._attach_resume(page, app, profile)
                self._banner(page, filled, unfilled)
                return self._wait_for_human(page, app, filled, unfilled)
            except Exception as exc:  # noqa: BLE001
                return ApplyResult.failure(f"assisted apply failed: {exc}")
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass

    # --- data ------------------------------------------------------------------
    def _values(self, app: Application, profile: Profile) -> dict[str, str]:
        first, last = self.split_name(profile)
        values = {
            "first_name": first,
            "last_name": last,
            "full_name": profile.full_name,
            "email": profile.email,
            "phone": profile.phone,
            "location": profile.location,
            "linkedin_url": profile.linkedin_url,
            "github_url": profile.github_url,
            "portfolio_url": profile.portfolio_url or profile.github_url,
            "cover_letter": app.cover_letter,
        }
        return {k: v for k, v in values.items() if v}

    # --- page interaction ------------------------------------------------------
    def _open_application_form(self, page) -> None:
        """Click through to the form if the landing page is just a job ad."""
        for selector in (
            "button.jobs-apply-button",                       # LinkedIn Easy Apply
            "a#apply_button", "a[href*='#application']",      # Greenhouse
            "a.postings-btn", "a[href*='/apply']",            # Lever
            "button:has-text('Apply Now')", "button:has-text('Apply for this job')",
            "a:has-text('Apply for this job')",
        ):
            try:
                button = page.locator(selector).first
                if button.count() and button.is_visible():
                    button.click(timeout=6000)
                    page.wait_for_timeout(2500)
                    return
            except Exception:  # noqa: BLE001
                continue

    def _fill(self, page, values: dict[str, str], app: Application,
              profile: Profile) -> tuple[list[str], list[str]]:
        filled, unfilled = [], []
        frames = [page] + [f for f in page.frames if f != page.main_frame]

        for frame in frames:
            try:
                inputs = frame.locator(
                    "input:not([type=hidden]):not([type=submit]):not([type=button]), "
                    "textarea, select"
                )
                count = min(inputs.count(), 80)
            except Exception:  # noqa: BLE001
                continue

            for i in range(count):
                field = inputs.nth(i)
                try:
                    if not field.is_visible() or not field.is_editable():
                        continue
                    label = self._label_for(frame, field)
                    if not label or SKIP_FIELD.search(label):
                        continue
                    if (field.input_value() or "").strip():
                        continue  # the browser profile already filled it

                    key = self._match_rule(label)
                    value = values.get(key, "") if key else ""

                    if not value and self.tailor is not None:
                        value = self.tailor.answer(app.job, label)

                    if value:
                        field.fill(value, timeout=5000)
                        filled.append(f"{label[:55]} = {value[:45]}")
                    else:
                        unfilled.append(label[:70])
                except Exception:  # noqa: BLE001
                    continue
        return filled, unfilled

    def _match_rule(self, label: str) -> Optional[str]:
        lowered = label.lower()
        for pattern, key in FIELD_RULES:
            if re.search(pattern, lowered):
                return key
        return None

    def _label_for(self, frame, field) -> str:
        """Best available human-readable name for a form field."""
        for getter in (
            lambda: field.get_attribute("aria-label"),
            lambda: field.get_attribute("placeholder"),
            lambda: field.get_attribute("name"),
            lambda: field.get_attribute("id"),
        ):
            try:
                if value := (getter() or "").strip():
                    # A <label for=id> beats the raw id if one exists.
                    if label := self._linked_label(frame, value):
                        return label
                    return value
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _linked_label(self, frame, field_id: str) -> str:
        try:
            label = frame.locator(f"label[for='{field_id}']").first
            if label.count():
                return (label.inner_text(timeout=2000) or "").strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _attach_resume(self, page, app: Application, profile: Profile) -> bool:
        path = self.resume_path(app, profile)
        if not path:
            return False
        for frame in [page] + [f for f in page.frames if f != page.main_frame]:
            try:
                uploads = frame.locator("input[type=file]")
                for i in range(min(uploads.count(), 4)):
                    upload = uploads.nth(i)
                    name = (upload.get_attribute("name") or upload.get_attribute("id") or "").lower()
                    if "cover" in name:
                        continue  # that slot wants the letter, not the CV
                    upload.set_input_files(path, timeout=15000)
                    page.wait_for_timeout(1500)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _banner(self, page, filled: list[str], unfilled: list[str]) -> None:
        """Inject a review panel so you can see what the agent did before submitting."""
        script = """
        (data) => {
          document.getElementById('jobagent-banner')?.remove();
          const box = document.createElement('div');
          box.id = 'jobagent-banner';
          box.style.cssText = `position:fixed;top:0;left:0;right:0;z-index:2147483647;
            background:#0f172a;color:#e2e8f0;font:13px/1.5 ui-sans-serif,system-ui,sans-serif;
            padding:12px 16px;box-shadow:0 2px 14px rgba(0,0,0,.4);max-height:38vh;overflow:auto`;
          const list = (items, color) => items.length
            ? `<ul style="margin:4px 0 0;padding-left:18px;color:${color}">` +
              items.map(t => `<li>${t.replace(/</g,'&lt;')}</li>`).join('') + '</ul>'
            : '<div style="color:#64748b;margin-top:4px">none</div>';
          box.innerHTML = `
            <strong style="color:#38bdf8">job-agent prefilled this form - review, then submit yourself.</strong>
            <div style="display:flex;gap:28px;margin-top:8px;flex-wrap:wrap">
              <div style="flex:1;min-width:260px"><b>Filled (${data.filled.length})</b>${list(data.filled,'#86efac')}</div>
              <div style="flex:1;min-width:260px"><b>Needs you (${data.unfilled.length})</b>${list(data.unfilled,'#fca5a5')}</div>
            </div>`;
          document.body.appendChild(box);
          document.body.style.paddingTop = box.offsetHeight + 'px';
        }
        """
        try:
            page.evaluate(script, {"filled": filled, "unfilled": unfilled})
        except Exception as exc:  # noqa: BLE001
            log.debug("banner injection failed: %s", exc)

    def _wait_for_human(self, page, app: Application, filled: list[str],
                        unfilled: list[str]) -> ApplyResult:
        """Block until you submit or close the tab."""
        print(f"\n  Browser open: {app.job.title} @ {app.job.company}")
        print(f"  Prefilled {len(filled)} fields, {len(unfilled)} need you.")
        print("  Review the form, submit it, then close the tab (or press Enter here).")
        for item in unfilled[:10]:
            print(f"    - unfilled: {item}")

        timeout_s = int(self.config.raw.get("apply", {}).get("assisted_timeout_s", 600))
        try:
            page.wait_for_event("close", timeout=timeout_s * 1000)
            closed = True
        except Exception:  # noqa: BLE001 - timeout means it is still open
            closed = False

        answer = input("\n  Did you submit this application? [y/N/s=skip] ").strip().lower()
        if answer == "y":
            return ApplyResult.success("submitted by you in the browser",
                                       details={"filled": len(filled), "tab_closed": closed})
        if answer == "s":
            return ApplyResult(ok=False, message="skipped by you", details={"skip": True})
        return ApplyResult.human("left unsubmitted - still in the queue")
