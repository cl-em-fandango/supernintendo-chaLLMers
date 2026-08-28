"""Git operations. Thin wrapper over external/git_cli (see CODING_STANDARDS §4)."""
from __future__ import annotations

from external.git_cli import (  # noqa: F401
    LAST_GOOD_TAG,
    GateNotApplicable,
    cleanup_branch,
    ensure_branch,
    merge_to_trunk,
    verify_harness,
)
