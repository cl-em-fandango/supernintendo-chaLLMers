# T25 — Queue-audit epic (superseded)

> **DO NOT EXECUTE THIS FILE AS A CARD.** Pure inventory/anomaly behavior is T60; CLI and report
> persistence are T61. This file is retained only as the parent contract.

**Wave 5** · depends: T21, T22, T23 · finding: F13 · **[decision D4 — recorded, see Context]**

## Context
The live queue is in a state the code cannot describe: `active/002-pipeline-checkpoint-and-resume`
has an **old-format** `task.json` (no `checkpointed_stages`, no `last_updated`, `stage: spec`) while
`artifacts/` already hold `spec.md`, `slices.md` and a feasibility kickback — a resume re-runs
spec/feasibility/slicing; it also contains a scratch `.git`. `claimed/` holds 7 tasks (003, 004, 005,
007, 008, `auto-3…`, `auto-4…`) that `status` did not show before T11; two `auto-*` bodies read as
truncated model monologue rather than requirements. `work/logs/other/` holds stray `.pi-session-*.out`
files, several 0 bytes.
**D4 answer on record:** "leave them where they are for now — we will have a review pass once these
changes are in place." So this card is **audit-only**. It was originally "queue surgery"; that is
cancelled. Its output is the input to the human's later review pass.

## Read first
- `harness/cli/handlers.py` — `cmd_status` (the row list, post-T11) and `_slug`
- `harness/core/providers.py` — `DirectoryTaskProvider`, `_slug`, `list_claims()` (T09): the
  id↔filename mismatch lives here (`003-keep-…md` → id `003_keep_…`)
- `harness/workflow/task_lifecycle.py` — `QUEUE_LOCATIONS`, `load_state`, `_exec_summary`
- `harness/core/config.py` — `queue_dir`, `logs_dir`

## Do
1. New module `harness/workflow/queue_audit.py` with
   `def audit_queue(cfg) -> list[str]` returning report lines, and
   `def render_audit(cfg) -> str`. **Read-only**: the only writes are to the report file itself.
2. The report must contain, in this order: per-directory counts for `pending, active, claimed, done,
   parked, failed, review`; then for every task dir, one row — `id`, directory, `status` from
   `task.json`, `checkpointed_stages`, `workdir` (T22 field, `—` when absent), and whether the dir
   contains a `.git`; then an `ANOMALY` line for each of: `status` disagrees with directory (T21
   check, retro-finds `parked/001` saying `active`), `task.json` missing/old-format, `.git` under the
   queue, `.pi-session-*.out` under the queue or in `logs_dir`, the same slug appearing in more than
   one lifecycle location, a claim with missing/corrupt ownership metadata after T46, and a task body
   shorter than 200 characters (the `auto-*` smell). A claimed-only file is normal claim state and
   must not be called orphaned merely because no pending copy exists.
3. Wire `harness.py queue-audit` (parser subcommand → `cli/handlers.cmd_queue_audit`) printing the
   report to stdout and writing it to `cfg.logs_dir / f"queue-audit-{date}.md"`. No `--yes`, no
   mutation flags, no backup logic — delete that from your mental model of the card.
4. Do **not** normalize `002`, do not remove its `.git`, do not requeue the 7 claims, do not delete
   any `.out` file. If you find yourself writing `shutil`, `os.remove` or `shutil.move` in this
   card, stop: that is the review pass D4 postponed, and it is not scheduled.
5. In the report footer, emit a `SUGGESTED OPERATOR ACTIONS` section listing the anomalies with the
   command an operator would run *by hand* (`git -C <dir> status`, `harness.py unpark <id>`, …). The
   harness never runs them.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, tempfile, json, hashlib, pathlib; sys.path.insert(0,'.')
from harness.core.config import Config
from harness.workflow.queue_audit import audit_queue, render_audit
root = pathlib.Path(tempfile.mkdtemp())
q = root/"queue"
for d in ("pending","active","claimed","done","parked","failed","review"): (q/d).mkdir(parents=True)
(q/"parked"/"t1").mkdir(); (q/"parked"/"t1"/"task.json").write_text(json.dumps(
    {"id":"t1","status":"active","source":"s","created":"x","stage":"spec","history":[]}))
(q/"active"/"t2").mkdir()                                    # no task.json -> anomaly
(q/"active"/"t2"/".git").mkdir()                             # scratch repo -> anomaly
(q/"claimed"/"t3-some-claim.md").write_text("short\n")        # orphan claim + short body
before = sorted(p.as_posix() for p in root.rglob("*"))
cfg = Config({"workDir": str(root)}, q)
txt = render_audit(cfg); lines = audit_queue(cfg)
after = sorted(p.as_posix() for p in root.rglob("*"))
assert before == after, "audit MUTATED the queue"
assert "t1" in txt and "parked" in txt
assert any("status disagrees" in l or "disagrees" in l for l in lines)
assert any(".git" in l for l in lines) and any("task.json" in l for l in lines)
assert any("claimed" in l for l in lines) and any("SUGGESTED OPERATOR ACTIONS" in l for l in lines)
print("queue audit ok, lines:", len(lines))
PY
before=$(find /home/donald/work/queue -printf '%p %s\n' | sort | sha256sum)
python3 harness.py queue-audit >/dev/null; echo "rc=$?"       # rc=0, and it must not move anything
after=$(find /home/donald/work/queue -printf '%p %s\n' | sort | sha256sum)
[ "$before" = "$after" ] && echo "real queue byte-identical ✓" || echo "AUDIT MUTATED THE QUEUE"
```
Writing the dated report into `cfg.logs_dir` is the PLAN §Rules exception to `AGENTS.MD`: a **new**
report file, nothing pre-existing touched. Nothing in this card writes under `queue_dir` at all.
Must pass, plus the Gate.

## Out of scope
Everything D4 postponed: requeueing, normalizing `002`'s `task.json`, deleting the scratch `.git`,
deleting `auto-3`/`auto-4`, truncating `supervisor.log` (T02 owns rotation and is already landed).
Also out: `requeue-claims` (T12 owns that command; this card may *suggest* it in the footer, never
call it), and changing what `cmd_status` prints (T11 owns it).

## Done when
`harness.py queue-audit` prints and logs the inventory; the audit code contains no write to any path
under `queue_dir` (verify by grep in the commit message); `/home/donald/work/queue` is byte-identical
before and after running it on the real config; the report flags `parked/001`'s stale `status`.
