# Refactor Chunk 6: Split harness.py into cli/ + thin composition root

## Context
CODING_STANDARDS.md §4: `cli/` only parses and dispatches; the top-level entry
point is the single composition root with no business logic. Today
`harness.py` (209 lines) mixes manual `sys.argv` dispatch, all the `cmd_*`
handlers, AND the `build()` composition root. This chunk separates them.

## Read first
- `CODING_STANDARDS.md` — §4
- `harness.py` — the whole file

## Do

Create `harness/cli/` with `__init__.py`.

### 6a. `harness/cli/parser.py` — argparse
Replace the manual `sys.argv` parsing in `main()` with a real
`argparse.ArgumentParser`. Subcommands and their args (match current behavior
exactly):
- `run`
- `run-task <file>`
- `run-one`
- `run-task-loop`
- `autonomous`
- `status`
- `report`
- `unpark <task-id>`  (also accept `requeue` as an alias)

`parser.py` exposes `build_parser() -> argparse.ArgumentParser` and
`parse_args(argv) -> Namespace`. It contains NO business logic — just the
parser definition.

### 6b. `harness/cli/handlers.py` — the cmd_* functions
Move every `cmd_*` function and its helpers (`_log`, `_requeue_claimed`,
`_slug`) into `harness/cli/handlers.py`. Each handler takes the already-built
dependencies (or builds them via the composition root — see 6c) and returns an
int exit code. Keep their bodies unchanged.

The handlers currently call the module-level `build()`. To keep the
composition root in one place, have `handlers.py` import `build` from the
composition root (6c) rather than defining it.

### 6c. `harness.py` — thin composition root
`harness.py` becomes:
- `CONFIG_PATH` constant
- `build(cfg_path=CONFIG_PATH)` — the existing composition root (builds
  Config, StatsStore, SessionRunner, provider, Pipeline). UNCHANGED logic.
- `main()` — calls `cli.parser.parse_args(sys.argv[1:])`, then dispatches to
  the matching `cli.handlers.cmd_*` function, returning its exit code.
- NO `cmd_*` functions, NO `_log`, NO `_slug`, NO `_requeue_claimed` here.

So `harness.py` should end up roughly: imports, `CONFIG_PATH`, `build()`,
`main()`, and the `if __name__ == "__main__"` guard. Target < 60 lines.

## Rules
- CLI behavior is byte-identical: same subcommands, same args, same exit
  codes, same log output.
- `cli/` may import from `workflow/`, `core/`, and the composition root
  (`harness.build`), never define business logic.
- `build()` stays the single place that wires dependencies together.

## Verify (the gate)
```
cd /home/donald/work/harness
# cli/ exists, harness.py is thin
ls harness/cli/*.py
test $(wc -l < harness.py) -lt 80 && echo "harness.py is thin ✓"
# no cmd_ handlers left in harness.py
! grep -qE "^def cmd_" harness.py && echo "no handlers in root ✓"
# full gate + real CLI invocations
python3 -c "import sys; sys.path.insert(0,'.'); import harness, harness.cli.parser, harness.cli.handlers; print('import ok')"
python3 harness.py status
python3 harness.py report >/dev/null && echo "report ok"
python3 harness.py unpark 2>&1 | head -1   # should print "not found", not crash
```
All must pass (the `unpark` line should print a "not found" message, rc may be
1 — that's correct behavior, not a failure).

## Commit
```
git add -A
git -c user.email=pi@harness.local -c user.name=pi-harness commit -m "harness: split CLI into cli/ (parser, handlers) + thin composition root"
```
Then: `git tag -f pi/last-good pi/trunk`

## Done when
- `harness/cli/parser.py` and `harness/cli/handlers.py` exist
- `harness.py` is < 80 lines: only `build()`, `main()`, constants, guard
- No `cmd_*`/`_log`/`_slug`/`_requeue_claimed` in `harness.py`
- Gate passes (import + status + report + unpark-no-crash)
- Committed and `pi/last-good` advanced
