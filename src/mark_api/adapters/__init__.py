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
from .monkrel_runtime import (
    MonkrelPrivateHttpRuntimeClient,
    build_monkrel_private_http_adapter,
    build_monkrel_private_http_runtime_client,
)

__all__ = [
    "BrowserBotAdapter",
    "BrowserBotError",
    "BrowserBotProcessError",
    "BrowserBotRuntime",
    "BrowserBotWorkspaceError",
    "ManagementReadAdapter",
    "MonkrelMobileApiAdapter",
    "MonkrelPrivateHttpContentClient",
    "MonkrelPrivateHttpRuntimeClient",
    "build_monkrel_private_http_adapter",
    "build_monkrel_private_http_runtime_client",
]
