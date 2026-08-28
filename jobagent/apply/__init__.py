"""Apply drivers, keyed by Job.apply_method."""
from __future__ import annotations

from ..config import Config
from .assisted import AssistedDriver
from .ats_api import GreenhouseDriver, LeverDriver
from .base import ApplyResult, BaseDriver

DRIVERS = {
    "greenhouse": GreenhouseDriver,
    "lever": LeverDriver,
    "assisted": AssistedDriver,
    "manual": AssistedDriver,
}


def get_driver(method: str, config: Config, tailor=None) -> BaseDriver:
    """Any unknown method falls back to the browser flow, which handles anything."""
    return DRIVERS.get(method, AssistedDriver)(config, tailor)


__all__ = ["ApplyResult", "BaseDriver", "DRIVERS", "get_driver",
           "AssistedDriver", "GreenhouseDriver", "LeverDriver"]
