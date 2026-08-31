"""Test-session defaults.

The FR-1 execution gate (`harness/core/environment.py`) makes the
entrypoints refuse bare-metal runs. Per the spec's edge cases, the repo's
own test harness sets the documented escape hatch so in-process tests that
load `harness.py`/`supervisor.py` keep working on a bare host or inside an
unrelated CI container. Individual gate tests override the environment as
needed.
"""
from __future__ import annotations

import os

os.environ.setdefault("HARNESS_ALLOW_HOST_UNSAFE", "1")
