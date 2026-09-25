"""Adapter implementations for external Kleinanzeigen surfaces."""

from .management import ManagementReadAdapter
from .monkrel import MonkrelMobileApiAdapter

__all__ = [
    "ManagementReadAdapter",
    "MonkrelMobileApiAdapter",
]
