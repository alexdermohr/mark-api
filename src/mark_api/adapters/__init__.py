"""Adapter implementations for external Kleinanzeigen surfaces."""

from .browser_bot import (
    BrowserBotAdapter,
    BrowserBotError,
    BrowserBotProcessError,
    BrowserBotRuntime,
    BrowserBotWorkspaceError,
)
from .management import ManagementReadAdapter
from .monkrel import MonkrelMobileApiAdapter
from .monkrel_http import MonkrelPrivateHttpContentClient

__all__ = [
    "BrowserBotAdapter",
    "BrowserBotError",
    "BrowserBotProcessError",
    "BrowserBotRuntime",
    "BrowserBotWorkspaceError",
    "ManagementReadAdapter",
    "MonkrelMobileApiAdapter",
    "MonkrelPrivateHttpContentClient",
]
