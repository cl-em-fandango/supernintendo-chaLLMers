# T40 — `pyproject.toml`, an advisory ruff config, and `scripts/gate.sh`

**Wave 9** · depends: T34–T39 · findings: F11, F12 · **[decision D5 — recorded, see Context]**

## Context
There is no `pyproject.toml`, no `requirements.txt`, no linter config and no CI config (F11): the
dependency set is tribal knowledge and the Gate lives only as a copy-paste block in
`PLAN-2026-08-26.md` that every card retypes (and every retyping is a chance to run a slightly
different gate).
**D5 answer on record:** "There is no CI/CD or even a git remote. Do not concern yourself with CI/CD."
So this card ships **no** `.github/workflows`, no Actions file, no git hook (a pre-push hook never
fires without a remote). What it ships is the parts that pay off with no CI at all: a declared
dependency set, an advisory lint config, and the Gate as a script.

## Read first
- `PLAN-2026-08-26.md` — the Gate block, verbatim: it is what `scripts/gate.sh` must reproduce
- `harness/core/*.py`, `external/*.py` — the actual third-party imports (expect: none — confirm with
  the grep in step 1 before writing the dependency list)
- `tests/` — the discover command and the current test count
- `README.md` — where the new commands get documented (T33 owns the prose; you add the two commands)

## Do
1. Establish the truth first: `grep -rn "^import \|^from " harness external harness.py supervisor.py
   tests | grep -v "harness\.\|external\.\|^\./tests" | sort -u` — the stdlib-only answer is a fact to
   put in the commit message, not an assumption.
2. `pyproject.toml`: `[project]` with `name = "harness"`, `requires-python = ">=3.14"` (measured
   3.14.7), `dependencies = []` if step 1 confirms stdlib-only, `[project.optional-dependencies]
   dev = ["ruff"]`. Add `[tool.ruff]` with `line-length` set to the de-facto width and a *narrow*
   `select` (e.g. `["F"]` — unused imports/variables, i.e. the F9 class this plan keeps hitting).
3. **Ruff is advisory in this card.** Run it, paste the residual count and the top rules into the
   commit message, and do **not** mass-edit files to satisfy it, and do not add rules that fail on the
   existing tree — wave 7 just landed a mechanical diff and a formatting sweep would make it
   unauditable. If ruff is not installed, note that and skip; the card must not add a hard dependency.
4. `scripts/gate.sh` (executable): the Gate from `PLAN-2026-08-26.md` byte-equivalent in behaviour —
   unittest discover, the import check, `python3 harness.py status` with the `rc=0` assertion — each
   step echoed, failing fast with a non-zero exit and a clear `GATE FAILED: <step>` line. No
   `git add`, no commit, no tag: the script checks, it never commits.
5. Document both in README (`scripts/gate.sh` and `python3 -m ruff check .`) and in each card-facing
   place that matters: add one line to the top of `PLAN-2026-08-26.md`'s Gate section saying
   "equivalently: `scripts/gate.sh`". Do not restate the Gate.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, tomllib, os, subprocess, pathlib
d = tomllib.loads(pathlib.Path('pyproject.toml').read_text())
assert d['project']['name'] == 'harness'
assert '3.14' in d['project']['requires-python'], d['project']['requires-python']
assert 'dev' in d.get('project', {}).get('optional-dependencies', {})
assert 'ruff' in str(d.get('tool', {}).get('ruff', '')) or 'tool.ruff' in str(d)
g = pathlib.Path('scripts/gate.sh')
assert g.exists() and os.access(g, os.X_OK), "scripts/gate.sh missing or not executable"
t = g.read_text()
assert 'git commit' not in t and 'tag -f' not in t, "gate.sh must not commit or tag"
for needle in ('unittest discover', 'harness.py status', 'import ok'):
    assert needle in t, f"gate.sh is missing the Gate step: {needle}"
rc = subprocess.run(['bash','scripts/gate.sh'], capture_output=True, text=True)
assert rc.returncode == 0, rc.stdout[-2000:] + rc.stderr[-2000:]
print("pyproject + gate script ok")
PY
```
Must pass, plus the Gate.

## Out of scope
**Any CI/CD at all — no `.github/`, no Actions, no git hooks, no pre-commit framework (decision D5).**
Also out: adopting pytest (the suite is `unittest` and stays `unittest`), enabling format-on-lint or
running `ruff format` across the tree, packaging/publishing (`build`, `twine`, wheel config), pinning
versions of things that do not exist, and touching `config.json`.

## Done when
`pyproject.toml` parses and states the real dependency set; `scripts/gate.sh` is executable and exits
0 on a clean tree and non-zero with a named step on a broken one; no CI/hook file exists anywhere;
README documents both commands; ruff's residual count is recorded in the commit message rather than
driven to zero.
