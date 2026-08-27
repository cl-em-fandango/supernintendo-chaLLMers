# T37 — Handler tests: `status` shows claims, `run-one` requeues, `requeue-claims`

**Wave 9** · depends: T09, T10, T11, T12 · finding: F11

## Context
`cli/handlers.py` is 166 lines of dispatch with zero tests, and it is where the F2 leak lived:
`cmd_run` claimed every `pending/*.md` into `claimed/` and never returned them, while `cmd_status`
did not list `claimed/` — seven tasks sat invisible for weeks. Wave 2 fixed the provider API (T09),
the leak (T10), the visibility (T11) and the reclaim command (T12) with hand-run snippets only. The
stub-`build` pattern used in the T10 and T12 verify blocks is proven and is what this card promotes
into permanent tests.

## Read first
- `harness/cli/handlers.py` — `cmd_run`, `cmd_run_one`, `cmd_run_task_loop`, `cmd_status`,
  `cmd_unpark`, `_requeue_claimed`, and the duplicated `_slug`
- `harness/core/providers.py` — `DirectoryTaskProvider`, `fetch_pending(claim=…)`, `list_claims`,
  `requeue_claim`, `requeue_all_claims` (T09)
- `plan-2026-08-26/T10-cmd-run-no-leak.md` and `T12-stale-claim-requeue.md` — their verify blocks are
  the tests to promote
- `harness/composition.py` — what `build()` returns (the 5-tuple) so the stub matches

## Do
1. New file `tests/test_handlers.py`. A `stub_build(tmp)` helper returns the same 5-tuple shape as
   `composition.build()` but with a temp queue, a fake provider and a pipeline that records calls
   instead of running sessions. Patch `harness.cli.handlers.H` (or wherever `build` is imported as)
   with `unittest.mock.patch`, restoring automatically.
2. Cases:
   **a.** `cmd_status` output contains a `claimed` row and its count, and a `pending` row — the T11
   regression test. Capture stdout with `contextlib.redirect_stdout`.
   **b.** `cmd_run` with 3 pending files and a pipeline that processes one leaves the other two
   **requeued** (back in `pending/`) or otherwise accounted for — assert against the post-T10
   behaviour, and assert `claimed/` is empty afterwards. This is the F2 regression test.
   **c.** `cmd_run_one` with 3 pending processes exactly one and requeues the rest (the existing
   `_requeue_claimed` path).
   **d.** `requeue-claims` (T12) with N claim files moves them all back to `pending/` with their
   original filenames, and is a no-op with an empty `claimed/` (must not raise).
   **e.** the id↔filename mismatch: a claim file `003-keep-x.md` whose task id is `003_keep_x` is
   matched correctly by `requeue_claim` — slug **both** sides.
   **f.** a handler never raises on a missing queue directory (composition normally mkdirs; call the
   handler directly).
3. No test may use the real `/home/donald/work/queue` — assert every path the handler touched is under
   the temp root, and set the config the handler reads to the temp `workDir`.
4. Do not test `cmd_run_task_loop`'s loop *body* beyond "it requeues and dispatches" — the loop's
   decision logic is T38's pure-function test.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, unittest, pathlib; sys.path.insert(0,'.')
p = pathlib.Path('tests/test_handlers.py'); assert p.exists()
src = p.read_text()
assert '/home/donald/work/queue' not in src, "handler test points at the real queue"
assert 'patch' in src, "no stub/patch of build()"
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_handlers')
assert suite.countTestCases() >= 5, f"only {suite.countTestCases()} cases"
r = unittest.TextTestRunner(verbosity=0).run(suite)
assert r.wasSuccessful(), r.failures + r.errors
print(f"handler tests ok ({suite.countTestCases()} cases)")
PY
```
Must pass, plus the Gate.

## Out of scope
The cycle decision and supervisor wiring (T38 — never import `supervisor` here), real sessions or a
real pipeline run, `resume`'s existing 10 CLI tests (already covered), argument-parsing details
beyond a subcommand existing, and touching `/home/donald/work/queue` — including its 7 `claimed/`
files, which stay exactly where they are (decision D4).

## Done when
`tests/test_handlers.py` has ≥5 green cases including the `status`-shows-claimed case and the
`cmd_run`-leaves-nothing-claimed case; all fixtures are temp dirs; the full suite passes twice in a
row with the real queue unchanged (`ls /home/donald/work/queue/claimed | wc -l` identical before and
after).
