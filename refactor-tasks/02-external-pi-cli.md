# Refactor Chunk 2: Isolate pi subprocess in external/pi_cli.py

## Context
CODING_STANDARDS.md §4: all subprocess calls live in `external/` behind small
function signatures; nothing else in the codebase shells out for pi. Today
`harness/session.py` calls `subprocess.Popen(["pi", ...])` directly. This chunk
extracts the raw "talk to pi" mechanics into `external/pi_cli.py` and makes
`SessionRunner` a thin policy wrapper around it.

## Read first
- `CODING_STANDARDS.md` — §4 "Clear modular boundaries"
- `harness/session.py` — the whole file (you will split it)

## The split

**`external/pi_cli.py`** owns the mechanics (no stats, no logging policy, no
Config):
- `HEARTBEAT_S`, `HARD_TIMEOUT_S` constants
- `@dataclass PiSessionResult` — the raw outcome: `rc`, `crashed`, `err`,
  `peak_tokens`, `duration_s`, `output` (the concatenated assistant text),
  `out_file`
- `run_pi_session(*, model, workdir, prompt, out_file, log) -> PiSessionResult`
  — the body of today's `SessionRunner.run()` from the `workdir = Path(workdir)`
  line down through the `proc`/heartbeat/streaming/reap block, returning a
  `PiSessionResult`. It takes a `log` callable for the heartbeat lines (so the
  external layer can stay decoupled from our logging format) and does NOT
  record stats or emit the `▶`/`◀` stage lines.
- `_extract_verdict(output) -> str` and `_now() -> str` move here too (they are
  pi-output mechanics, not policy).

**`harness/session.py`** keeps the policy:
- `SessionResult` dataclass (unchanged — this is our internal shape)
- `SessionRunner` with `__init__(cfg, store, log)` unchanged
- `SessionRunner.run(...)` becomes:
  1. emit the `▶` log line
  2. compute `out_file`
  3. call `external.pi_cli.run_pi_session(model=..., workdir=..., prompt=...,
     out_file=..., log=self.log)`
  4. extract verdict from the result's `output` (call
     `external.pi_cli._extract_verdict` — or expose it as `extract_verdict`)
  5. record the `SessionRecord` in `self.store` (unchanged logic)
  6. emit the `◀` log line
  7. return `SessionResult` (unchanged shape)
- `_outcome(verdict)` stays in `session.py` (it maps verdicts to stat outcomes —
  that's stats policy, not pi mechanics).

## Rules
- `external/pi_cli.py` must NOT import from `harness.config`, `harness.stats`,
  or anything in the workflow layer. It may import stdlib only.
- `harness/session.py` must no longer contain `subprocess` or the raw `pi`
  command list.
- Behavior must be identical: same heartbeat cadence, same timeout, same
  verdict extraction, same stats record fields.

## Verify (the gate)
```
cd /home/donald/work/harness
# external layer is isolated (no harness imports)
python3 -c "import sys; sys.path.insert(0,'.'); import external.pi_cli; print('external.pi_cli ok')"
# session no longer shells out directly
! grep -q "subprocess" harness/session.py && echo "session.py: no subprocess ✓"
# full gate
python3 -c "import sys; sys.path.insert(0,'.'); import harness, harness.session, external.pi_cli; print('import ok')"
python3 harness.py status
```
All must pass.

## Commit
```
git add -A
git -c user.email=pi@harness.local -c user.name=pi-harness commit -m "harness: isolate pi subprocess in external/pi_cli.py"
```
Then: `git tag -f pi/last-good pi/trunk`

## Done when
- `external/pi_cli.py` exists, imports stdlib only
- `harness/session.py` has no `subprocess` and no raw `pi` command
- `SessionRunner.run` delegates to `external.pi_cli.run_pi_session`
- Gate passes (import + status)
- Committed and `pi/last-good` advanced
