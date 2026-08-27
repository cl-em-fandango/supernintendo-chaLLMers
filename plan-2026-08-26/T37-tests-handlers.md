# T37 — Handler tests: `status` shows claims, `run-one` requeues, `requeue-claims`

**Wave 9** · depends: T09, T10, T11, T12 (case **h** additionally wants **T41** — see Do 2h) ·
finding: F11

## Context
`cli/handlers.py` is 166 lines of dispatch with zero tests, and it is where the F2 leak lived:
`cmd_run` claimed every `pending/*.md` into `claimed/` and never returned them, while `cmd_status`
did not list `claimed/` — seven tasks sat invisible for weeks. Wave 2 fixed the provider API (T09),
the leak (T10), the visibility (T11) and the reclaim command (T12) with hand-run snippets only. The
stub-`build` pattern used in the T10 and T12 verify blocks is proven and is what this card promotes
into permanent tests.
`cli/parser.py` and `workflow/autonomous.py` are the remaining two entries in F11's untested list and
**no other card owns either of them** — cases **g** and **h** below are the only coverage they get
anywhere in this plan, so they are not optional.

## Read first
- `harness/cli/handlers.py` — `cmd_run`, `cmd_run_one`, `cmd_run_task_loop`, `cmd_status`,
  `cmd_unpark`, and the duplicated `_slug`. **`_requeue_claimed` does not exist any more** — T09
  deletes it; claim recovery is `provider.requeue_claim`, and that is the path these tests assert on.
- `harness/core/providers.py` — `DirectoryTaskProvider`, `fetch_pending(claim=…, limit=…)`,
  `list_claims`, `requeue_claim`, `requeue_all_claims`, `claim_age_hours` (T09)
- `plan-2026-08-26/T10-cmd-run-no-leak.md` and `T12-stale-claim-requeue.md` — their verify blocks are
  the tests to promote (both were rewritten after T10/T12 changed shape — promote those, not the
  older wording in this card's history)
- `harness/cli/parser.py` (whole, 66 lines) and `harness.py`'s dispatch — for case **g**
- `harness/workflow/autonomous.py` — `_pending_count()` — for case **h**
- `harness/composition.py` — what `build()` returns **today, including how many elements**, so the
  stub matches (see Do 1: this is not the 5-tuple the audit counted)

## Do
1. New file `tests/test_handlers.py`. A `stub_build(tmp)` helper returns **the same shape as
   `composition.build()` at the moment you run this card** — count the unpack in
   `harness/cli/handlers.py`/`harness.py` and match it. Do not copy an arity out of card text: the
   original 5-tuple grew to **6** when **T07** appended the log sink, and a short stub against a 6-way
   unpack is a `ValueError` in this file, not in T07. Build the tuple once (one `_tuple(...)` helper)
   so a future arity change is a one-line edit, and note the arity you matched in the commit message.
   Everything else about the stub is unchanged: a temp queue, a fake provider, and a pipeline that
   records calls instead of running sessions. Patch `harness.cli.handlers.H` (or wherever `build` is
   imported as) with `unittest.mock.patch`, restoring automatically.
2. Cases:
   **a.** `cmd_status` output contains a `claimed` row and its count, and a `pending` row — the T11
   regression test. Capture stdout with `contextlib.redirect_stdout`.
   **b.** The F2 regression test, written against **post-T10** behaviour. The pre-T10 shape this card
   originally described ("claim everything, process one, leave the other two requeued") no longer
   exists: `cmd_run` claims **one at a time** with `fetch_pending(claim=True, limit=1)`. So: seed 3
   pending files *plus one foreign claim file this run must not touch*, and a pipeline stub that
   records what it processed and raises on the first task. Assert **all 3 were attempted, in order**,
   and that `claimed/` afterwards holds **exactly** the foreign file — own claims returned, foreign
   claim untouched (decision D4). Mirror T10's corrected assertions: counts and order, never the
   spelling of a task id.
   **c.** `cmd_run_one` with 3 pending processes exactly one and returns the other two to `pending/`
   through T09's `provider.requeue_claim` (the path that replaced the deleted `_requeue_claimed`);
   a foreign claim file again stays where it is.
   **d.** `requeue-claims` (T12) with N claim files moves them all back to `pending/` with their
   original filenames, and is a no-op with an empty `claimed/` (must not raise).
   **e.** the id↔filename mismatch: a claim file `003-keep-x.md` whose task id is `003_keep_x` is
   matched correctly by `requeue_claim` — slug **both** sides.
   **f.** a handler never raises on a missing queue directory (composition normally mkdirs; call the
   handler directly).
   **g.** **parser ↔ handler surface** — F11's `cli/parser` half. Enumerate the subcommands declared
   in `harness/cli/parser.py` (drive `parser.parse_args` over each one's minimal argv — do not
   retype the list by hand) and assert each resolves to a `cmd_*` handler that exists in
   `harness.cli.handlers`, and that no `cmd_*` handler is unreachable from the parser. A subcommand
   added to the parser without a dispatch arm in `harness.py` must fail here. This is the
   machine-checkable version of T16's usage list; it pins T16 rather than duplicating it.
   **h.** **`autonomous` cannot claim** — F11's `workflow/autonomous` half, and the only test of
   **T41** item 5's *call site* (T41's own verify exercises `count_pending` on the provider alone).
   With N pending files in a temp queue, calling the generator's pending-count path
   (`AutonomousGenerator._pending_count()`, which after T41 reads `provider.count_pending()`) returns
   N and moves nothing: `pending/` still holds N, `claimed/` holds exactly what it started with. Assert
   "counting performs no filesystem move", not the absence of a symbol name. If T41 has not landed,
   mark the case `@unittest.skipUnless(hasattr(TaskProvider, "count_pending"), "T41 not landed")` and
   record the pull-forward in the commit message — do not delete the case.
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
assert 'parser' in src, "no parser↔handler surface case (Do 2g): cli/parser stays untested"
assert 'autonomous' in src.lower() and 'count_pending' in src, \
    "no autonomous-cannot-claim case (Do 2h): workflow/autonomous stays untested"
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_handlers')
assert suite.countTestCases() >= 8, f"only {suite.countTestCases()} cases (a-h)"
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
`tests/test_handlers.py` has ≥8 green cases (a–h) including the `status`-shows-claimed case, the
`cmd_run`-leaves-nothing-of-its-own-claimed case, and both F11 gap cases **g** (parser ↔ handler) and
**h** (`autonomous` cannot claim) — nothing else in this plan tests those two modules; all fixtures
are temp dirs; the stub `build()` arity was read from the code, not from this card; the full suite
passes twice in a row with the real queue unchanged (`ls /home/donald/work/queue/claimed | wc -l`
identical before and after).
