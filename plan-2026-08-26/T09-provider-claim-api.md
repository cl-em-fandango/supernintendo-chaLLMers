# T09 — Give the provider a real claim API (`list_claims` / `requeue_*`)

**Wave 2** · depends: T01 · finding: F2

## Context
`DirectoryTaskProvider.fetch_pending(claim=True)` moves **every** `pending/*.md` into
`claimed/`. Only `run-one` and `run-task-loop` put the extras back, via `_requeue_claimed`
which lives in `cli/handlers.py` and matches by slug — i.e. recovery logic for the provider's
own side effect is implemented outside the provider, badly, and not used by `cmd_run` at all.
Result: 7 tasks are sitting in `queue/claimed/` right now and nothing can see them.

## Read first
- `harness/core/providers.py` — `DirectoryTaskProvider` (whole class), `_slug`
- `harness/cli/handlers.py` — `_requeue_claimed` (the thing this replaces)
- `ls /home/donald/work/queue/claimed/` (read-only! do not move anything in this card)

## Do
1. On `DirectoryTaskProvider` add:
   - `list_claims() -> list[Task]` — the files currently in `claimed/`, in sorted order, `source=f"claimed:{name}"`.
   - `requeue_claim(self, name_or_task) -> str | None` — move one claimed file back to
     `pending/` by filename or `Task`; return the new path string, or `None` if not found.
     Name collision in `pending/` → append `-requeued.md`, never overwrite.
   - `requeue_all_claims() -> list[str]` — requeue everything, return the moved names.
   - `claim_age_hours(name) -> float` — `(now - mtime)/3600` of a claimed file; `-1.0` if absent.
2. `fetch_pending(claim=True)` must now be **claim-one-safe at the caller's request**: add
   `limit: int | None = None` so a caller can claim only the first N. Default `None` keeps today's
   behavior (this card must not change behavior for existing callers).
3. Delete `handlers._requeue_claimed` and point its two call sites at
   `provider.requeue_claim(other)`. If `build()` there does not expose the provider by name,
   use the existing tuple position.
4. Abstract `TaskProvider` gets `list_claims()`/`requeue_all_claims()` as non-abstract defaults
   returning `[]` so non-directory providers stay valid.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, pathlib, tempfile
sys.path.insert(0,'.')
from harness.core.providers import DirectoryTaskProvider
q = pathlib.Path(tempfile.mkdtemp()); (q/"pending").mkdir(); (q/"claimed").mkdir()
(q/"pending"/"001-a.md").write_text("A"); (q/"pending"/"002-b.md").write_text("B")
p = DirectoryTaskProvider(q/"pending", q/"claimed")
t = p.fetch_pending(claim=True, limit=1)
assert len(t) == 1 and len(p.list_claims()) == 1, [x.id for x in t]
assert len(list((q/"pending").glob("*.md"))) == 1, "limit over-claimed"
assert p.requeue_claim(t[0]) is not None and len(p.list_claims()) == 0
(q/"pending"/"003-c.md").write_text("C"); (q/"claimed"/"004-d.md").write_text("D")
moved = p.requeue_all_claims(); assert moved == ["004-d.md"], moved
assert (q/"pending"/"004-d.md").exists()
assert p.claim_age_hours("004-d.md") >= 0 and p.claim_age_hours("nope.md") == -1.0
print("claim api ok")
PY
! grep -n "_requeue_claimed" harness/cli/handlers.py && echo "handler shim gone ✓"
```
Both must pass, plus the Gate (existing 40+ tests unchanged).

## Out of scope
`cmd_run`'s loop shape (T10), `status` output (T11), stale-claim policy/thresholds (T12),
moving any real queue file, concurrency/locking design.

## Done when
Repro prints `claim api ok`; `_requeue_claimed` no longer exists anywhere; claim recovery is
entirely the provider's responsibility.
