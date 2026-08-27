# T16 — Make the `--continue` docstrings state the real contract, and fix `harness.py`'s usage list

**Wave 3** · depends: T14 · finding: F1

## Context
Before T14, `cmd_run_one` (handlers.py:66-71) and `cmd_run_task_loop` (handlers.py:88-92) both
claimed "the supervisor calls this with `--continue`" while `supervisor.py:203` spawned plain
`run-one`. A docstring that describes an intention instead of a contract is how the F1 wiring
survived three slices of resume work. After T14 the claim is true but unproven — so write it as a
contract, name the card whose test proves it, and list the commands the entrypoint actually has
(`harness.py`'s docstring omits `run-one`, `unpark`, `requeue-claims` and every flag).

## Read first
- `harness/cli/handlers.py` — `cmd_run_one` 63-83, `cmd_run_task_loop` 86-105, `cmd_run` 44-59
- `harness.py` — module docstring 1-11, `main()` dispatch 28-55
- `harness/cli/parser.py` — the authoritative subcommand+flag list (whole file, 66 lines)
- `supervisor.py` — the T14 work block, to quote the real spawn

## Do
1. `cmd_run_one` docstring: delete the supervisor sentence. Say what it is — "claim and process at
   most one pending task, then exit; requeued extras are returned to `pending/`". Add
   "Not used by the supervisor."
2. `cmd_run_task_loop` docstring: state the contract as three lines — (a) `--continue` resumes every
   `active/` task that has a `task.json` before touching `pending/`; (b) tasks are claimed one at a
   time and the loop exits when `pending/` is empty; (c) this is the subcommand the supervisor spawns
   each cycle, contract proven by card T38's cycle test. Keep it under 8 lines.
3. `cmd_run` docstring (new, one sentence): processes pending tasks one at a time and then enters
   autonomous mode — the difference from `run-task-loop`.
4. `harness.py` module docstring: rewrite the usage list from `parser.py` only — every subcommand
   with its real flags (`run --continue`, `run-task <file> [--continue|--fresh]`, `run-one`,
   `run-task-loop --continue`, `autonomous`, `status`, `report`, `resume <task_id> [--yes]`,
   `unpark <task_id>`). Do **not** document `requeue-claims` if T12 has not landed; check.
5. No behavior change anywhere: docstrings only, `git diff --stat` must show insertions ≈ deletions.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import ast, pathlib, sys
h = pathlib.Path("harness/cli/handlers.py").read_text()
assert "Not used by the supervisor" in h, "cmd_run_one still advertises itself to the supervisor"
assert "T38" in h, "run-task-loop contract does not name its proof"
assert h.count("the supervisor calls this") == 0, "old claim still present"
doc = ast.get_docstring(ast.parse(pathlib.Path("harness.py").read_text())) or ""
for cmd in ("run-one", "run-task-loop", "unpark", "resume", "autonomous", "status", "report"):
    assert cmd in doc, f"usage list missing {cmd}"
assert "--continue" in doc and "--fresh" in doc and "--yes" in doc, "flags undocumented"
# the docstring must not invent commands: every listed subcommand exists in the parser
import subprocess
for cmd in ("run","run-task","run-one","run-task-loop","autonomous","status","report","resume","unpark"):
    rc = subprocess.run([sys.executable,"harness.py",cmd,"--help"],capture_output=True)
    assert rc.returncode == 0, f"{cmd} --help failed"
print("docstrings ok")
PY
```
Must pass, plus the Gate.

## Out of scope
README (T33 owns the operator-facing docs — there is no T43; card ids stop at T42), `parser.py` help
strings, the supervisor's own
docstring (T14 touched it), adding `requeue-claims` if T12 is not landed, and any behavior change —
this card may not alter a single executable line.

## Done when
Neither `cmd_run_one` nor `cmd_run_task_loop` misdescribes the supervisor; `cmd_run_task_loop`'s
docstring names `T38` as the proof of the `--continue` contract; `harness.py`'s usage list matches
`parser.py` exactly for the 9 landed subcommands; the diff is docstring-only.
