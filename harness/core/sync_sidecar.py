"""The legacy GitHub-linkage sidecar format (spec FR-1.6, pre-record).

Before each task had one metadata record (`task_record.py`), its GitHub
linkage lived in a file derived from the task's *path*:

* a task *file* (`pending/X.md`, `claimed/X.md`, ...) carried `X.md.gh.json`
  next to it;
* an active/terminal *task directory* carried `gh.json` inside it.

Nothing writes or reads that format any more — the record is the only linkage
store, and `task_record` has its own legacy reader for migration. What remains
here is the format's *vocabulary*: the two file-name suffixes, so the
migration reader is the only production module that can derive a metadata path
from a task-file name (acceptance criteria 3 and 5). A queue written before
the record still holds these files; a queue written after it never does.

The shape a sidecar holds is `SyncLinkage` (`sync_linkage.py`) — the record's
`github` section and the legacy payload are the same thing, so the dataclass
lives apart from both stores.

Seeding a pre-record queue (tests, hand repair of an old queue) needs the
paths and the writer; they live in `tests/legacy_sidecars.py`, outside the
production package, so no production code path can create a legacy file.
"""
from __future__ import annotations

# `pending/X.md` -> `pending/X.md.gh.json` (beside the task file).
SIDECAR_SUFFIX = ".gh.json"
# An active task dir carries its own `gh.json`.
TASK_DIR_SIDECAR_NAME = "gh.json"
