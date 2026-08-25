# Refactor Chunk 4: Relocate leaf modules into core/

## Context
CODING_STANDARDS.md §4 wants a clear layering: `cli → workflow → core →
external → nothing`. The leaf/data modules currently sit at the `harness/`
package root. This chunk moves them into a `core/` subpackage. It is a PURE
MOVE — no logic changes — so it is low-risk but touches many import lines.

## Read first
- `CODING_STANDARDS.md` — §4
- `REFACTOR_PLAN.md` — "Target structure"

## Do

Create `harness/core/` and move these files into it (use `git mv` to preserve
history):
- `harness/config.py`    → `harness/core/config.py`
- `harness/stats.py`     → `harness/core/stats.py`
- `harness/providers.py` → `harness/core/providers.py`
- `harness/prompts.py`   → `harness/core/prompts.py`
- `harness/session.py`   → `harness/core/session.py`
- `harness/gitops.py`    → `harness/core/gitops.py`
- `harness/enums.py`     → `harness/core/enums.py`

Create `harness/core/__init__.py` (empty or a one-line docstring).

Then update every import that referenced the old locations. The affected
importers are:
- `harness/pipeline.py` — `from .config import Config`, `from .gitops import
  ...`, `from .providers import Task`, `from .session import SessionRunner`,
  `from . import prompts`  →  change to `from .core.config import Config`,
  `from .core.gitops import ...`, `from .core.providers import Task`,
  `from .core.session import SessionRunner`, `from .core import prompts`
- `harness/autonomous.py` — `from . import prompts`, `from .config import
  Config`, `from .providers import Task`, `from .session import SessionRunner`
  →  same `.core.` prefix
- `harness/core/session.py` (after move) — its internal `from .config import
  Config` and `from .stats import ...` become `from .config import Config` and
  `from .stats import ...` (unchanged, since they're now siblings in core/).
  BUT its `from external.pi_cli import ...` (added in chunk 2) must become an
  absolute import `from external.pi_cli import ...` (it already is, since
  external is a top-level package) — verify it still resolves.
- `harness/core/gitops.py` (after move) — its `from external.git_cli import ...`
  stays absolute. Verify it resolves.
- `harness.py` (top level) — `from harness.config import load`,
  `from harness.providers import Task, create_provider`,
  `from harness.session import SessionRunner`,
  `from harness.stats import StatsStore, render_report`  →  change to
  `from harness.core.config import load`, `from harness.core.providers import
  Task, create_provider`, `from harness.core.session import SessionRunner`,
  `from harness.core.stats import StatsStore, render_report`
- `supervisor.py` — `from harness.providers import create_provider` and
  `from harness.config import load`  →  `from harness.core.providers import
  create_provider`, `from harness.core.config import load`

## Rules
- No logic changes. Only file locations and import paths change.
- `external/` is untouched (it's already a top-level package).
- After the move, `harness/` root should contain only: `__init__.py`,
  `core/`, `pipeline.py`, `autonomous.py` (workflow modules — chunk 5 moves
  those), and the new `core/` package.

## Verify (the gate)
```
cd /home/donald/work/harness
# old locations are gone
! test -e harness/config.py && ! test -e harness/session.py && echo "old files moved ✓"
# new locations exist
test -d harness/core && ls harness/core/*.py | wc -l
# full import + status
python3 -c "import sys; sys.path.insert(0,'.'); import harness, harness.core.config, harness.core.session, harness.core.gitops, harness.pipeline, harness.autonomous; print('import ok')"
python3 harness.py status
python3 harness.py report >/dev/null && echo "report ok"
```
All must pass.

## Commit
```
git add -A
git -c user.email=pi@harness.local -c user.name=pi-harness commit -m "harness: move leaf modules into core/ subpackage"
```
Then: `git tag -f pi/last-good pi/trunk`

## Done when
- All 7 leaf modules live in `harness/core/`
- No file at the old `harness/<name>.py` locations
- Gate passes (import + status + report)
- `supervisor.py` still imports correctly
- Committed and `pi/last-good` advanced
