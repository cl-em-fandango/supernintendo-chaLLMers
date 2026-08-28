# T11 — `harness.py status` must show `claimed/`

**Wave 2** · depends: T09 · finding: F2

## Context
`cmd_status` (handlers.py:114-122) iterates `("pending","active","done","failed","parked","review")`.
`claimed/` is not in the list, so the 7 stranded tasks are invisible to the only inspection tool
in the repo — the leak could not be found by looking. `build()` also `mkdir`s the six queue dirs
but not `claimed`, so the dir is created only as a side effect of constructing a provider.

## Read first
- `harness/cli/handlers.py` — `cmd_status`
- `harness/composition.py` — the `for sub in (...)` mkdir loop
- `harness/core/providers.py` — T09's `list_claims()`, `claim_age_hours()`

## Do
1. Add a `claimed` row to `cmd_status`, rendered as
   `claimed     (7): 003-keep-rejected… (48h), 004-model-refresh (48h), …`
   — age in whole hours from `claim_age_hours()`, `-` when there are none.
2. Keep the row ordering lifecycle-shaped: `pending, claimed, active, review, parked, failed, done`.
3. `composition.build()` mkdirs `claimed` too (derive the tuple from one shared constant rather
   than repeating the list — put `QUEUE_LOCATIONS_ALL` next to `QUEUE_LOCATIONS` in
   `harness/workflow/task_lifecycle.py` and import it in both places).
4. If `claimed/` is non-empty, print one extra warning line under the table:
   `⚠ N claimed tasks: nothing will process them until they are requeued (plan card T12,
   'requeue-claims').` Name the card, **not** the command as a runnable thing: T12 lands after this
   card, and a message that tells an operator to type a subcommand which does not exist yet is the
   same lie T16 exists to remove. T12 updates this line to name the command when it ships it.

## Verify
```bash
cd /home/donald/work/harness
python3 harness.py status | tee /tmp/t11.txt | head -20
for row in pending claimed active review parked failed done; do
  grep -q "^$row " /tmp/t11.txt || { echo "MISSING ROW: $row"; exit 1; }
done
echo "all rows present ✓"
python3 - <<'PY'
import sys, pathlib, tempfile, io, contextlib
sys.path.insert(0,'.')
import harness.cli.handlers as H
from harness.core.providers import DirectoryTaskProvider
q = pathlib.Path(tempfile.mkdtemp())
for sub in ("pending","active","done","failed","parked","review","claimed"): (q/sub).mkdir()
(q/"claimed"/"009-stuck.md").write_text("x")
import types
cfg = types.SimpleNamespace(queue_dir=q, logs_dir=q/"logs", stats_path=q/"s.jsonl")
# 6-tuple: build() gained the log sink in T07 — match the real unpack in handlers.py
H.build = lambda *a, **k: (cfg, __import__("harness.core.stats", fromlist=["StatsStore"]).StatsStore(cfg.stats_path), None, DirectoryTaskProvider(q/"pending", q/"claimed"), None, lambda line="": None)
buf = io.StringIO()
with contextlib.redirect_stdout(buf): H.cmd_status()
out = buf.getvalue()
assert "claimed" in out and "009-stuck" in out, out
assert "T12" in out, "warning line missing (must name plan card T12, not an unlanded command)"
print("status claims row ok")
PY
```
All must pass, plus the Gate.

## Out of scope
Implementing `requeue-claims` (T12 — this card only *mentions* it), changing the stats report,
moving any file.

## Done when
`harness.py status` prints a `claimed` row with ages and the warning line while the real
`claimed/` is non-empty; the dir list exists in exactly one place in the codebase.
