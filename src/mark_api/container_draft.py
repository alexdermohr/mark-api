"""Read-only marker that disables direct Python CLI entrypoints in Draft images.

The marker is created root-owned and non-writable during the reviewed Docker
image build. Normal non-container CLI use is unaffected. Removing this draft
denial requires a new independently attested release, not a runtime option.
"""
from __future__ import annotations

import os
from pathlib import Path


_MARKER = Path("/etc/mark-api/UNATTESTED_DRAFT")


def container_draft_denied() -> bool:
    try:
        os.lstat(_MARKER)
    except FileNotFoundError:
        return False
    except OSError:
        # Unexpected inability to read the marker is not permission to start.
        return True
    return True
