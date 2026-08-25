# Coding Standards

Guiding principles for all coding agents working in this repo. These are
binding: new and changed code must follow them. When a standard and a quick
hack conflict, follow the standard — the harness is long-lived and read far
more often than it is written.

## 1. One responsibility per file, named after that responsibility

A file does one thing and its name says what that thing is. No grab-bag
modules. If a file is doing two jobs, split it.

- Good: `session.py` (runs one pi session), `gitops.py` (git branch/merge
  helpers), `stats.py` (the stats store), `providers.py` (task sources).
- Bad: one `harness.py` that runs sessions, does git, computes stats, and
  parses the CLI.

When you add a capability, ask: does an existing file already own this? If
not, give it its own file named for the responsibility.

## 2. State and behavior are split

Data shape lives in a `@dataclass` (or plain class); the functions that act on
it live in a separate module. No tuples, no bare dicts for meaningful state —
every piece of state is a named class with typed fields.

- `SessionResult` (dataclass) holds the shape of a finished session;
  `SessionRunner.run()` in `session.py` produces it.
- `Task` (dataclass) holds a task's shape; `providers.py` builds them.
- `SessionRecord` (dataclass) is one row of stats; `StatsStore` in `stats.py`
  appends and queries them.

If you find yourself passing a `dict` or a tuple around to represent something
real, stop and give it a class.

## 3. Enums instead of magic strings for state

Discrete states are `enum.Enum` members, not bare strings scattered through
the code.

- Task lifecycle: `TaskStatus.ACTIVE / DONE / PARKED / FAILED` — not `"active"`,
  `"parked"`, etc.
- Session verdicts: `Verdict.PASS / FAIL / KICKBACK / ...` — not `"pass"`,
  `"fail"`.

Strings are fine at the very edges (the `VERDICT:` line a model emits, a git
branch name), but the moment that value moves inside our code it becomes an
enum member.

## 4. Clear modular boundaries

Dependencies point one direction and never shell out or render directly from
the middle of the graph.

- **`external/`** is the boundary for anything outside the process. All
  subprocess calls live here behind small function signatures:
  - `external/pi_cli.py` — spawn a pi session, stream its JSON, return a
    `SessionResult`. Nothing else in the codebase calls `subprocess` for pi.
  - `external/git_cli.py` — the git branch/merge/verify/revert operations.
    Nothing else calls `subprocess` for git.
- **`cli/`** only parses and dispatches. `cli/parser.py` builds the
  `argparse` parser; `cli/handlers.py` maps subcommands to workflow calls. It
  contains no business logic.
- **`workflow/`** composes the smaller modules into the actual pipeline loop
  (`pipeline.py`, `autonomous.py`), taking an explicit parameters object rather
  than a long argument list or a global.
- **`harness.py`** (top level) is the single composition root: it builds the
  config, wires the modules together, and dispatches. No business logic.
- Every module imports only what it needs. Dependency direction:
  `cli → workflow → (session/stats/providers) → external → nothing`.
  `external` and leaf data modules import nothing from `workflow` or `cli`.

## 5. Explicit parameters objects, not long argument lists or globals

A workflow entry point takes one named parameters object, not six positional
args. Configuration is loaded once at the composition root and passed down;
there is no module-level mutable global that holds "the current config".

## 6. Small, verifiable steps

- Keep functions short and single-purpose; if a function needs a comment to
  explain its *why*, it is probably doing too much.
- Every change to the harness must pass the verification gate
  (`import harness` + `harness.py status`) — that is the minimum bar for
  "works".
- Prefer boring, readable code over clever. The next agent (often a fresh
  model with no memory of this conversation) has to understand it cold.

## 7. Naming

- `snake_case` for functions, methods, variables, modules.
- `PascalCase` for classes and dataclasses.
- `UPPER_SNAKE_CASE` for module-level constants and enum members.
- Names say what a thing *is* or *does*, not its type (`task_dir`, not
  `td`; `peak_tokens`, not `n`).
