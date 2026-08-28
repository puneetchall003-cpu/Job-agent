"""Source plumbing: a shared HTTP client, country inference, and the registry."""
from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional

import requests

from ..config import Config
from ..models import Job

log = logging.getLogger(__name__)

USER_AGENT = "job-agent/1.0 (personal job search; +https://github.com/)"

# Location string -> country bucket. Ordered: first hit wins, so put the
# unambiguous city names before the country names they sit in.
_COUNTRY_HINTS: list[tuple[str, str]] = [
    (r"\b(bengaluru|bangalore|hyderabad|pune|mumbai|delhi|gurgaon|gurugram|noida|"
     r"chennai|kolkata|ahmedabad|jaipur|kochi|indore|india)\b", "IN"),
    (r"\b(london|manchester|birmingham|edinburgh|glasgow|bristol|leeds|cambridge|"
     r"oxford|reading|belfast|cardiff|united kingdom|england|scotland|wales|"
     r"\buk\b|great britain)\b", "GB"),
    (r"\b(new york|san francisco|seattle|austin|boston|chicago|denver|atlanta|"
     r"los angeles|dallas|houston|miami|phoenix|portland|san jose|washington|"
     r"united states|usa|u\.s\.|remote us|nyc)\b", "US"),
]

# US state codes appear as ", CA" / ", TX" in most US postings.
_US_STATE = re.compile(
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|"
    r"WV|WI|WY|DC)\b", re.I)

_REMOTE = re.compile(r"\b(remote|work from home|wfh|distributed|anywhere)\b", re.I)


def infer_country(*parts: str) -> str:
    """Best-effort country bucket from any location-ish strings."""
    blob = " ".join(p for p in parts if p).lower()
    for pattern, code in _COUNTRY_HINTS:
        if re.search(pattern, blob):
            return code
    if _US_STATE.search(blob):
        return "US"
    return ""


def is_remote(*parts: str) -> bool:
    return bool(_REMOTE.search(" ".join(p for p in parts if p)))


def strip_html(html: str) -> str:
    """Good-enough HTML -> text. Job descriptions are the only input here."""
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&rsquo;": "'", "&ndash;": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"&[a-z#0-9]{2,8};", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class HttpClient:
    """requests wrapper with retry/backoff and a polite delay between calls."""

    def __init__(self, delay: float = 0.6, timeout: int = 25, retries: int = 3):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._last_call = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_call = time.time()

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                log.warning("GET %s failed (%s), attempt %d", url, exc, attempt + 1)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = int(resp.headers.get("Retry-After") or 2 ** (attempt + 1))
                log.warning("GET %s -> %d, backing off %ds", url, resp.status_code, wait)
                time.sleep(min(wait, 30))
                continue
            if resp.status_code >= 400:
                log.warning("GET %s -> %d", url, resp.status_code)
                return None
            return resp
        return None

    def get_json(self, url: str, **kwargs) -> Any:
        resp = self.get(url, **kwargs)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("Non-JSON response from %s", url)
            return None


class Source(ABC):
    """A place to find jobs. Subclasses only implement `fetch`."""

    name: str = "base"
    # True when the source needs credentials the user must supply.
    requires_credentials: bool = False

    def __init__(self, config: Config, http: Optional[HttpClient] = None):
        self.config = config
        self.conf = config.source_conf(self.name)
        self.http = http or HttpClient()

    @abstractmethod
    def fetch(self) -> Iterable[Job]:
        """Yield normalised jobs. Must not raise on network failure."""

    def collect(self) -> list[Job]:
        """Run `fetch`, swallowing failures so one dead source can't kill the run."""
        try:
            jobs = [j for j in self.fetch() if j and j.title and j.company]
        except Exception as exc:  # noqa: BLE001 - a broken source must not abort the run
            log.error("Source %s failed: %s", self.name, exc, exc_info=True)
            return []
        log.info("Source %s returned %d jobs", self.name, len(jobs))
        return jobs


_REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    _REGISTRY[cls.name] = cls
    return cls


def available_sources() -> dict[str, type[Source]]:
    return dict(_REGISTRY)


def build_sources(config: Config, http: Optional[HttpClient] = None) -> list[Source]:
    """Instantiate every source enabled in config."""
    http = http or HttpClient()
    built = []
    for name, cls in _REGISTRY.items():
        if config.source_enabled(name):
            built.append(cls(config, http))
    return built
