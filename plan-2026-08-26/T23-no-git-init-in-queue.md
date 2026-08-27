# T23 — Refuse to create or use a git repo inside the queue tree

**Wave 5** · depends: T22 · finding: F7 · `[tag]`

## Context
`external/git_cli.ensure_branch(workdir, task_id, trunk)` does `git init -b trunk` plus a
"harness: initial commit" when the workdir has no `.git`. Combined with `resolve_workdir`'s
fall-back to the task directory (T22), that means a task that does not name a repo silently creates
a real git repository **inside** `/home/donald/work/queue/active/<id>/`. Confirmed live:
`queue/active/002-pipeline-checkpoint-and-resume/.git` exists with exactly that commit, its sessions
ran against it, and its `.pi-session-*.out` files landed in the queue. Session output and git objects
in the queue are the root of half of wave 5.

## Read first
- `external/git_cli.py` — `ensure_branch` (the `git init` branch in particular), `_git`, `_has`
- `harness/workflow/pipeline.py` — `process()`: `workdir = …` then `ensure_branch(workdir, …)`, and
  the `except` that parks the task
- `harness/core/config.py` — the `queue_dir` property (the guard needs it, and `external/` must not
  import config semantics it does not need)
- `plan-2026-08-26/T22-record-workdir.md` — where the workdir now comes from

## Do
1. Add a pure predicate to `external/git_cli.py`:
   `def is_under_queue(workdir: Path, queue_dir: Path) -> bool` — resolve both, return
   `queue_dir in workdir.parents or workdir == queue_dir`. No config import, no logging.
2. The guard belongs in `workflow/pipeline.process()` — it is the only caller that knows both the
   workdir and `cfg.queue_dir`, and keeping it there means `git_cli` stays a dumb git wrapper
   (CODING_STANDARDS: `external/` owns subprocess, `workflow/` composes). **State this choice in the
   commit message.** Do not add a `deny_prefix` parameter to `ensure_branch`.
3. In `process()`, immediately after the workdir is resolved and **before** `ensure_branch`: if
   `is_under_queue(workdir, cfg.queue_dir)` → do not call `ensure_branch`; `park` the task with a
   reason naming both paths, e.g. `refusing to init a repo in the queue: workdir=<w> is under
   queue=<q>; record the real repo path in the task body`, and return.
4. The park reason must reach the operator: it goes through the existing `park(...)` →
   `_exec_summary` path, so `resume <id>` shows it. Do not invent a new channel.
5. Also refuse when the resolved workdir is not a directory at all — same branch, different reason
   string. Do not `mkdir` a workdir.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, tempfile, pathlib; sys.path.insert(0,'.')
from external.git_cli import is_under_queue
q = pathlib.Path("/home/donald/work/queue").resolve()
assert is_under_queue(q/"active"/"002", q) is True
assert is_under_queue(q, q) is True
assert is_under_queue("/tmp/real/repo", q) is False
assert is_under_queue("/tmp/queue-totally-unrelated", q) is False   # prefix string trick
# end-to-end: a task with no repo in its body must park, and must not create a .git anywhere
root = pathlib.Path(tempfile.mkdtemp())
for d in ("pending","active","parked","failed","done","claimed","review"): (root/"queue"/d).mkdir()
t = root/"queue"/"active"/"t1"; t.mkdir(); (t/"original.md").write_text("no repo named here at all\n")
from harness.core.config import Config
from harness.workflow.task_lifecycle import TaskLifecycle
cfg = Config({"workDir": str(root)}, root/"queue")
lc = TaskLifecycle(cfg, log=lambda *a: None); lc.intake("t1","no repo named here at all","test")
git_dirs = list(root.rglob(".git"))
assert not git_dirs, f"a .git appeared in the queue: {git_dirs}"
print("queue git guard ok")
PY
```
Must pass, plus the Gate. Then `git tag -f pi/last-good pi/trunk`.

## Out of scope
What the verification gate should *be* for a non-harness repo (T24, and per decision **D3** that
question is explicitly deferred — this card only stops the bleeding inside the queue), removing the
existing scratch `.git` from `queue/active/002…` (operator action, **deferred by D4**, do not touch
`/home/donald/work/queue`), where session `.out` files should live instead of the workdir (nobody
owns it yet — mention it in the commit message and stop), and any change to `merge_to_trunk`.

## Done when
`is_under_queue` exists and the four assertions above pass; a task with no repo path in its body gets
parked with a reason naming both paths; no code path can `git init` under `cfg.queue_dir`; the live
queue is unmodified (`git status` in the *harness* repo shows only this card's files).
