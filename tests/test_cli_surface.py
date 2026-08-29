"""T68 — the CLI surface: every subcommand reaches a handler, every handler is reachable.

`cli/parser.py` declares the subcommands, `harness.py:main()` maps each of them
to a `cli/handlers.cmd_*` function, and nothing checks the two against each
other. Both failure modes are silent: a subcommand added to the parser without a
dispatch branch parses fine, then falls through to the usage text and returns 1;
a handler renamed or left behind by a refactor keeps its tests green while no
command can ever call it. This file owns neither side's behavior — it asserts
only that the surface is closed in both directions.

The dispatch table is read from `harness.py`'s AST rather than imported, so the
composition root is never executed and no run path is entered.

Covered here:
  * every subcommand the parser accepts has a dispatch branch, and that branch
    names at least one handler;
  * every handler a branch names exists in `cli/handlers.py` and is callable;
  * no public `cmd_*` handler in `cli/handlers.py` is unreachable from `main()`;
  * the dispatch branches cover no command the parser cannot produce;
  * the hidden `requeue` alias reaches the same handler as `unpark`.
"""
from __future__ import annotations

import argparse
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.cli import handlers
from harness.cli.parser import build_parser

_HARNESS_PY = Path(__file__).resolve().parent.parent / "harness.py"


def _parser_subcommands() -> set[str]:
    """Every subcommand name `build_parser()` accepts, aliases included."""
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("cli/parser.py builds no subcommand parser")


def _tests_args_command(test: ast.expr) -> bool:
    """True for the left-hand side of `args.command == ...` / `args.command in ...`."""
    return (isinstance(test, ast.Attribute) and test.attr == "command"
            and isinstance(test.value, ast.Name) and test.value.id == "args")


def _condition_commands(test: ast.expr) -> set[str]:
    """Command names one `if`/`elif` condition tests `args.command` against.

    Reads both `== "run"` and the `in ("unpark", "requeue")` alias form. Any
    other comparison — `args.command is None` — names no command and yields
    nothing.
    """
    if not isinstance(test, ast.Compare) or not _tests_args_command(test.left):
        return set()
    if not all(isinstance(op, (ast.Eq, ast.In)) for op in test.ops):
        return set()
    found: set[str] = set()
    for comparator in test.comparators:
        for node in ast.walk(comparator):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


def _branch_handlers(body: list[ast.stmt]) -> set[str]:
    """The `handlers.cmd_*` names a dispatch branch calls."""
    found: set[str] = set()
    for stmt in body:
        for node in ast.walk(stmt):
            if (isinstance(node, ast.Attribute) and node.attr.startswith("cmd_")
                    and isinstance(node.value, ast.Name) and node.value.id == "handlers"):
                found.add(node.attr)
    return found


def _dispatch_table() -> dict[str, set[str]]:
    """command -> the handlers `main()` dispatches it to, read from harness.py."""
    tree = ast.parse(_HARNESS_PY.read_text())
    main = next((node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "main"), None)
    if main is None:
        raise AssertionError("harness.py defines no top-level main()")
    table: dict[str, set[str]] = {}
    for node in ast.walk(main):
        if not isinstance(node, ast.If):
            continue
        targets = _branch_handlers(node.body)
        for command in _condition_commands(node.test):
            table.setdefault(command, set()).update(targets)
    return table


def _public_handlers() -> set[str]:
    """Every public `cmd_*` callable defined in `cli/handlers.py`."""
    return {name for name, value in vars(handlers).items()
            if name.startswith("cmd_") and callable(value)}


class CliSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.subcommands = _parser_subcommands()
        self.table = _dispatch_table()

    def test_parser_declares_subcommands(self):
        self.assertTrue(self.subcommands, "parser exposes no subcommands at all")

    def test_every_subcommand_has_a_dispatch_branch(self):
        for command in sorted(self.subcommands):
            with self.subTest(command=command):
                self.assertIn(command, self.table,
                              f"{command} parses but main() never dispatches it")
                self.assertTrue(self.table[command],
                                f"main() dispatches {command} to no handler")

    def test_every_dispatch_target_exists_and_is_callable(self):
        for command, targets in sorted(self.table.items()):
            for handler in sorted(targets):
                with self.subTest(command=command, handler=handler):
                    target = getattr(handlers, handler, None)
                    self.assertIsNotNone(target,
                                         f"{command} dispatches to missing "
                                         f"handlers.{handler}")
                    self.assertTrue(callable(target),
                                    f"handlers.{handler} is not callable")

    def test_no_public_handler_is_unreachable(self):
        reachable = {handler for targets in self.table.values() for handler in targets}
        unreachable = sorted(_public_handlers() - reachable)
        self.assertEqual([], unreachable,
                         "public handlers no subcommand can reach: "
                         + ", ".join(unreachable))

    def test_no_dispatch_branch_for_an_unknown_command(self):
        unknown = sorted(set(self.table) - self.subcommands)
        self.assertEqual([], unknown,
                         "main() dispatches commands the parser cannot produce: "
                         + ", ".join(unknown))

    def test_requeue_alias_reaches_the_unpark_handler(self):
        self.assertIn("requeue", self.subcommands,
                      "the hidden requeue alias is not registered with the parser")
        self.assertEqual(self.table["unpark"], self.table["requeue"])
        self.assertEqual({"cmd_unpark"}, self.table["unpark"])


if __name__ == "__main__":
    unittest.main()
