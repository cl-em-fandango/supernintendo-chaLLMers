"""FR-1 container entrypoint gate tests.

Covers `harness/core/environment.py`: all four detection markers, the
unreadable-`/proc` fallback, the refusal message and exit code, the
`HARNESS_ALLOW_HOST_UNSAFE=1` escape hatch (exact value only, warning
printed once), and the wiring of `assert_containerized()` into the two
entrypoints (read from their ASTs, never executed — the same pattern as
`test_cli_surface.py` / `test_cycle_decision.py`).

All filesystem checks run against temp-dir fakes; no subprocesses.
"""
from __future__ import annotations

import ast
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core import environment  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# A 64-hex container id as cgroup v2 lines carry it.
FAKE_CONTAINER_ID = "a" * 64


class _FakeFs(unittest.TestCase):
    """Base with temp root/proc fakes and a clean environment dict."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.proc = self.root / "proc"
        self.proc.mkdir(parents=True)
        environment.reset_host_unsafe_warning()

    def env(self, **extra: str) -> dict:
        # No container markers, no escape hatch unless a test adds one.
        base = {k: v for k, v in os.environ.items()
                if k not in ("container", environment.ESCAPE_ENV_VAR)}
        base.update(extra)
        return base

    def write_cgroup(self, text: str) -> None:
        one = self.proc / "1"
        one.mkdir(parents=True, exist_ok=True)
        (one / "cgroup").write_text(text, encoding="utf-8")


class DetectionTest(_FakeFs):
    def test_no_markers_is_not_containerized(self):
        self.assertFalse(environment.is_containerized(
            root=self.root, proc_root=self.proc, env=self.env()))

    def test_dockerenv_marker(self):
        (self.root / ".dockerenv").touch()
        self.assertTrue(environment.is_containerized(
            root=self.root, proc_root=self.proc, env=self.env()))

    def test_containerenv_marker(self):
        run = self.root / "run"
        run.mkdir()
        (run / ".containerenv").touch()
        self.assertTrue(environment.is_containerized(
            root=self.root, proc_root=self.proc, env=self.env()))

    def test_container_env_var_podman(self):
        self.assertTrue(environment.is_containerized(
            root=self.root, proc_root=self.proc,
            env=self.env(container="podman")))

    def test_container_env_var_docker(self):
        self.assertTrue(environment.is_containerized(
            root=self.root, proc_root=self.proc,
            env=self.env(container="docker")))

    def test_container_env_var_empty_does_not_count(self):
        self.assertFalse(environment.is_containerized(
            root=self.root, proc_root=self.proc,
            env=self.env(container="")))

    def test_cgroup_runtime_markers(self):
        for marker in ("docker", "kubepods", "containerd", "libpod"):
            with self.subTest(marker=marker):
                proc = Path(self._tmp.name) / f"proc-{marker}"
                one = proc / "1"
                one.mkdir(parents=True)
                (one / "cgroup").write_text(
                    f"0::/system.slice/{marker}.scope/\n", encoding="utf-8")
                self.assertTrue(environment.is_containerized(
                    root=self.root, proc_root=proc, env=self.env()))

    def test_cgroup_container_id_path_best_effort(self):
        self.write_cgroup(f"0::/{FAKE_CONTAINER_ID}\n")
        self.assertTrue(environment.is_containerized(
            root=self.root, proc_root=self.proc, env=self.env()))

    def test_cgroup_root_path_is_not_containerized(self):
        self.write_cgroup("0::/\n")
        self.assertFalse(environment.is_containerized(
            root=self.root, proc_root=self.proc, env=self.env()))

    def test_unreadable_proc_is_not_containerized_and_never_raises(self):
        # /proc replaced by a regular file: every read under it fails.
        proc = Path(self._tmp.name) / "not-a-dir"
        proc.write_text("x", encoding="utf-8")
        self.assertFalse(environment.is_containerized(
            root=self.root, proc_root=proc, env=self.env()))

    def test_missing_proc_is_not_containerized(self):
        self.assertFalse(environment.is_containerized(
            root=self.root,
            proc_root=self.root / "absent-proc",
            env=self.env()))


class GateTest(_FakeFs):
    def _run_gate(self, env: dict, entrypoint: str = "harness.py") -> str:
        """Call the gate with sys.stderr captured; SystemExit propagates."""
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            environment.assert_containerized(entrypoint, root=self.root,
                                             proc_root=self.proc, env=env)
        return buf.getvalue()

    def test_bare_host_exits_1_with_instructions(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            with self.assertRaises(SystemExit) as cm:
                environment.assert_containerized("harness.py",
                                                 root=self.root,
                                                 proc_root=self.proc,
                                                 env=self.env())
        self.assertEqual(cm.exception.code, 1)
        stderr_text = buf.getvalue()
        self.assertIn("harness.py", stderr_text)
        self.assertIn("scripts/harness-run", stderr_text)
        self.assertIn(environment.ESCAPE_ENV_VAR, stderr_text)

    def test_escape_hatch_exact_one_proceeds_with_warning(self):
        text = self._run_gate(self.env(**{environment.ESCAPE_ENV_VAR: "1"}))
        self.assertIn("WARNING", text)
        self.assertIn("harness.py", text)

    def test_warning_printed_once_across_calls(self):
        env = self.env(**{environment.ESCAPE_ENV_VAR: "1"})
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            for _ in range(3):
                environment.assert_containerized("harness.py",
                                                 root=self.root,
                                                 proc_root=self.proc,
                                                 env=env)
        self.assertEqual(buf.getvalue().count("WARNING"), 1)

    def test_other_escape_values_still_exit(self):
        for value in ("0", "true", "yes", "1 ", ""):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as cm:
                    self._run_gate(self.env(
                        **{environment.ESCAPE_ENV_VAR: value}))
                self.assertEqual(cm.exception.code, 1)

    def test_containerized_proceeds_without_warning(self):
        (self.root / ".dockerenv").touch()
        self.assertEqual(self._run_gate(self.env()), "")

    def test_containerized_with_escape_set_stays_silent(self):
        (self.root / ".dockerenv").touch()
        self.assertEqual(
            self._run_gate(self.env(**{environment.ESCAPE_ENV_VAR: "1"})), "")


class EntrypointWiringTest(unittest.TestCase):
    """Both entrypoints call assert_containerized() first in main()."""

    def _main_body(self, filename: str) -> list:
        tree = ast.parse((REPO_ROOT / filename).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                return node.body
        self.fail(f"{filename} defines no top-level main()")

    def _check_first_statement(self, filename: str) -> None:
        body = self._main_body(filename)
        first = body[0]
        self.assertIsInstance(first, ast.Expr,
                              f"{filename}: main() does not start with a call")
        call = first.value
        self.assertIsInstance(call, ast.Call,
                              f"{filename}: main() does not start with a call")
        self.assertIsInstance(call.func, ast.Name)
        self.assertEqual(call.func.id, "assert_containerized",
                         f"{filename}: main() must call "
                         f"assert_containerized() before any other work")

    def test_harness_py_gates_before_argument_parsing(self):
        self._check_first_statement("harness.py")

    def test_supervisor_py_gates_before_any_dispatch(self):
        self._check_first_statement("supervisor.py")

    def test_entrypoints_import_the_gate(self):
        for filename in ("harness.py", "supervisor.py"):
            src = (REPO_ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("from harness.core.environment import "
                          "assert_containerized", src,
                          f"{filename} does not import the gate")


if __name__ == "__main__":
    unittest.main()
