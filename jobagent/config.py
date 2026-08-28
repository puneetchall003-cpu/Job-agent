"""Configuration loading: YAML file + environment variables for secrets."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")

# Countries the agent knows how to search. Keys are what sources bucket on.
SUPPORTED_COUNTRIES = {
    "IN": {"name": "India", "adzuna": "in", "currency": "INR"},
    "GB": {"name": "United Kingdom", "adzuna": "gb", "currency": "GBP"},
    "US": {"name": "United States", "adzuna": "us", "currency": "USD"},
}

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in loaded YAML."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) or "")
        return _ENV_PATTERN.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


@dataclass
class SearchConfig:
    titles: list[str] = field(default_factory=list)
    exclude_titles: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=lambda: ["IN", "GB", "US"])
    locations: dict[str, list[str]] = field(default_factory=dict)
    remote_only: bool = False
    include_remote: bool = True
    max_age_days: int = 21
    min_salary: dict[str, float] = field(default_factory=dict)
    exclude_companies: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=lambda: ["lead", "principal", "staff", "senior", "manager", "head"])


@dataclass
class MatchConfig:
    min_score: float = 60.0
    use_llm: bool = True
    llm_top_n: int = 25          # only re-rank the best N with the LLM (cost control)
    llm_min_score: float = 65.0  # LLM verdict must also clear this


@dataclass
class ApplyConfig:
    enabled: bool = False        # master safety switch; nothing submits while False
    daily_limit: int = 15
    auto_methods: list[str] = field(default_factory=lambda: ["greenhouse", "lever"])
    assisted_methods: list[str] = field(default_factory=lambda: ["assisted"])
    require_confirmation: bool = True
    browser_profile_dir: str = ".browser-profile"
    headless: bool = False       # assisted mode must be visible: you click Submit


@dataclass
class NotifyConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_addr: str = ""
    to_addr: str = ""


@dataclass
class Config:
    profile: dict[str, Any] = field(default_factory=dict)
    search: SearchConfig = field(default_factory=SearchConfig)
    match: MatchConfig = field(default_factory=MatchConfig)
    apply: ApplyConfig = field(default_factory=ApplyConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    sources: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)
    data_dir: str = "data"
    raw: dict[str, Any] = field(default_factory=dict)

    # --- convenience accessors -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "applications.db"

    @property
    def output_dir(self) -> Path:
        return Path(self.data_dir) / "applications"

    def source_enabled(self, name: str) -> bool:
        conf = self.sources.get(name)
        if conf is None:
            return False
        if isinstance(conf, bool):
            return conf
        return bool(conf.get("enabled", True))

    def source_conf(self, name: str) -> dict[str, Any]:
        conf = self.sources.get(name)
        return conf if isinstance(conf, dict) else {}

    @property
    def anthropic_api_key(self) -> str:
        return self.llm.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

    @property
    def llm_model(self) -> str:
        return self.llm.get("model") or "claude-sonnet-5"


def load_config(path: str | Path | None = None) -> Config:
    """Load config.yaml, expanding ${ENV_VAR} references."""
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Copy config.example.yaml to config.yaml and edit it."
        )
    data = _expand_env(yaml.safe_load(path.read_text()) or {})
    return from_dict(data)


def from_dict(data: dict[str, Any]) -> Config:
    def build(cls, key):
        section = data.get(key) or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in section.items() if k in known})

    cfg = Config(
        profile=data.get("profile") or {},
        search=build(SearchConfig, "search"),
        match=build(MatchConfig, "match"),
        apply=build(ApplyConfig, "apply"),
        notify=build(NotifyConfig, "notify"),
        sources=data.get("sources") or {},
        llm=data.get("llm") or {},
        data_dir=data.get("data_dir") or "data",
        raw=data,
    )
    unknown = [c for c in cfg.search.countries if c not in SUPPORTED_COUNTRIES]
    if unknown:
        raise ValueError(
            f"Unsupported countries {unknown}; pick from {sorted(SUPPORTED_COUNTRIES)}"
        )
    return cfg
