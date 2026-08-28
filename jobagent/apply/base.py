"""Apply-driver contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..config import Config
from ..models import Application, Profile


@dataclass
class ApplyResult:
    ok: bool
    message: str = ""
    needs_human: bool = False       # partially filled; you must finish it
    confirmation: str = ""          # reference id / URL if the ATS returned one
    details: dict = field(default_factory=dict)

    @classmethod
    def failure(cls, message: str, **details) -> "ApplyResult":
        return cls(ok=False, message=message, details=details)

    @classmethod
    def success(cls, message: str, confirmation: str = "", **details) -> "ApplyResult":
        return cls(ok=True, message=message, confirmation=confirmation, details=details)

    @classmethod
    def human(cls, message: str, **details) -> "ApplyResult":
        return cls(ok=False, message=message, needs_human=True, details=details)


class Driver(Protocol):
    method: str

    def submit(self, app: Application, profile: Profile,
               dry_run: bool = True) -> ApplyResult: ...


class BaseDriver:
    method = "base"

    def __init__(self, config: Config, tailor=None):
        self.config = config
        self.tailor = tailor

    def split_name(self, profile: Profile) -> tuple[str, str]:
        parts = (profile.full_name or "").split()
        if not parts:
            return "", ""
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], " ".join(parts[1:])

    def resume_path(self, app: Application, profile: Profile) -> Optional[str]:
        path = app.resume_file or profile.resume_path
        if not path:
            return None
        from pathlib import Path
        return path if Path(path).exists() else None
