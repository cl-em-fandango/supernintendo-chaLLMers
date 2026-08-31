"""FR-3 ``scripts/set-llm-oom-priority.sh`` tests.

Drives the script with a fake ``pgrep`` on ``PATH`` and a temp-dir fake
``/proc`` root (``HARNESS_PROC_DIR``) — spec §9: no real processes are
touched, no real ``/proc`` writes. The PATH is a sanitized temp dir holding
only ``bash`` plus the fake ``pgrep``, so discovery is fully deterministic.

Covers: ``-1000`` written to each discovered PID across all three server
names, dedup across patterns, self-match exclusion (the script's own PID is
never protected), vanished and unwritable ``/proc`` entries warning without
aborting at exit 0, zero-PID idempotency, the one-line summary, missing-
pgrep as an unexpected error, and script surface (executable, ``bash -n``,
``set -u``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "set-llm-oom-priority.sh"

FAKE_PGREP = """#!/usr/bin/env bash
pattern="${@: -1}"
case "$pattern" in
    llama-server) pids="$FAKE_LLAMA_PIDS" ;;
    vllm)         pids="$FAKE_VLLM_PIDS" ;;
    ollama)       pids="$FAKE_OLLAMA_PIDS" ;;
    *)            pids="" ;;
esac
# SELF stands for the script's own PID: pgrep runs inside a command-
# substitution subshell, so the grandparent is the script process itself.
script_pid="$(ps -o ppid= -p "$PPID" | tr -d ' ')"
echo "$script_pid" > "$FAKE_PGREP_SEES_FILE"
for p in $pids; do
    if [ "$p" = "SELF" ]; then echo "$script_pid"; else echo "$p"; fi
done
"""


class SetLlmOomPriorityScriptTest(unittest.TestCase):
    """Temp fake /proc tree, sanitized PATH with a fake pgrep."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

        self.bindir = self.base / "bin"
        self.bindir.mkdir()
        for tool in ("bash", "ps", "tr"):
            os.symlink(shutil.which(tool), self.bindir / tool)
        self.pgrep_sees = self.base / "pgrep-saw.pid"
        self._install_fake_pgrep()

        self.proc = self.base / "proc"
        self.proc.mkdir()

    def _install_fake_pgrep(self) -> None:
        pgrep = self.bindir / "pgrep"
        pgrep.write_text(FAKE_PGREP)
        pgrep.chmod(0o755)

    def make_pid(self, pid: str, initial: str = "0") -> Path:
        entry = self.proc / pid
        entry.mkdir()
        score = entry / "oom_score_adj"
        score.write_text(initial + "\n")
        return score

    def make_unwritable_pid(self, pid: str) -> Path:
        entry = self.proc / pid
        entry.mkdir()
        # A directory named oom_score_adj can never be opened for writing,
        # even by root — a portable "unwritable" seam.
        score = entry / "oom_score_adj"
        score.mkdir()
        return score

    def run_script(
        self,
        llama: str = "",
        vllm: str = "",
        ollama: str = "",
        *,
        with_pgrep: bool = True,
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": str(self.bindir),
            "HARNESS_PROC_DIR": str(self.proc),
            "FAKE_LLAMA_PIDS": llama,
            "FAKE_VLLM_PIDS": vllm,
            "FAKE_OLLAMA_PIDS": ollama,
            "FAKE_PGREP_SEES_FILE": str(self.pgrep_sees),
        }
        if not with_pgrep:
            (self.bindir / "pgrep").unlink()
        return subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    # -- script surface ------------------------------------------------------

    def test_executable_bit_set(self) -> None:
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_bash_syntax_clean(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uses_set_u(self) -> None:
        self.assertIn("set -u", SCRIPT.read_text())

    def test_header_documents_sudo_systemd(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn("sudo", text)
        self.assertIn("systemd", text)

    # -- FR-3.1 / FR-3.2: discovery and protection ----------------------------

    def test_all_three_server_types_get_minus_one_thousand(self) -> None:
        llama_score = self.make_pid("101")
        vllm_score = self.make_pid("202")
        ollama_score = self.make_pid("303")
        result = self.run_script(llama="101", vllm="202", ollama="303")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(llama_score.read_text().strip(), "-1000")
        self.assertEqual(vllm_score.read_text().strip(), "-1000")
        self.assertEqual(ollama_score.read_text().strip(), "-1000")

    def test_multiple_pids_per_pattern_all_protected(self) -> None:
        scores = [self.make_pid(pid) for pid in ("11", "12", "13")]
        result = self.run_script(llama="11 12 13")
        self.assertEqual(result.returncode, 0, result.stderr)
        for score in scores:
            self.assertEqual(score.read_text().strip(), "-1000")

    def test_duplicate_pid_across_patterns_protected_once(self) -> None:
        shared_score = self.make_pid("500")
        other_score = self.make_pid("501")
        self.make_pid("502")
        result = self.run_script(llama="500 501", vllm="500 502")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(shared_score.read_text().strip(), "-1000")
        self.assertEqual(other_score.read_text().strip(), "-1000")
        # The summary counts unique PIDs only.
        self.assertIn("3 PID(s) protected", result.stdout)
        self.assertEqual(result.stdout.count("500"), 1)

    def test_script_self_pid_never_protected(self) -> None:
        result = self.run_script(llama="SELF")
        self.assertEqual(result.returncode, 0, result.stderr)
        script_pid = self.pgrep_sees.read_text().strip()
        # The fake pgrep reports the script's own PID; it must be filtered.
        self.assertNotIn(script_pid, result.stdout)
        self.assertFalse((self.proc / script_pid / "oom_score_adj").exists())

    def test_non_numeric_pgrep_output_ignored(self) -> None:
        result = self.run_script(llama="not-a-pid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 PID(s) protected", result.stdout)

    # -- FR-3.2: per-PID failures warn, never abort ----------------------------

    def test_vanished_pid_warns_without_abort(self) -> None:
        good_score = self.make_pid("101")
        result = self.run_script(llama="999 101")  # 999 has no /proc entry
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(good_score.read_text().strip(), "-1000")
        self.assertIn("999", result.stderr)
        self.assertIn("Warning", result.stderr)

    def test_unwritable_entry_warns_without_abort(self) -> None:
        good_score = self.make_pid("101")
        self.make_unwritable_pid("777")
        result = self.run_script(llama="777 101")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(good_score.read_text().strip(), "-1000")
        self.assertIn("777", result.stderr)

    def test_all_failures_still_exit_zero(self) -> None:
        result = self.run_script(llama="998 999")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 skipped", result.stdout)

    # -- FR-3.2: zero PIDs is idempotent success --------------------------------

    def test_no_pids_found_exits_zero_idempotent(self) -> None:
        for _ in range(2):
            result = self.run_script()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("0 PID(s) protected", result.stdout)

    # -- FR-3.3: one-line summary ------------------------------------------------

    def test_summary_lists_protected_and_skipped(self) -> None:
        self.make_pid("101")
        result = self.run_script(llama="101", ollama="999")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary_lines = [
            line for line in result.stdout.splitlines() if "PID(s) protected" in line
        ]
        self.assertEqual(len(summary_lines), 1)
        summary = summary_lines[0]
        self.assertIn("1 PID(s) protected [101]", summary)
        self.assertIn("skipped [999]", summary)

    # -- unexpected error: no pgrep at all ---------------------------------------

    def test_missing_pgrep_exits_nonzero(self) -> None:
        result = self.run_script(llama="101", with_pgrep=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pgrep", result.stderr)


if __name__ == "__main__":
    unittest.main()
