"""FR-2 `scripts/harness-run` tests.

Drives the runner script with ``HARNESS_DRY_RUN=1`` and fake container
engines on ``PATH`` (spec §9: never a real podman/docker run). The PATH is a
sanitized temp dir holding only the coreutils the script needs plus fake
``podman``/``docker`` executables, so a real engine can never be picked up.
The CPU-count fail-fast check is pinned via ``HARNESS_SYSFS_DIR`` fakes.

Covers: engine detection and ``HARNESS_ENGINE`` override, workdir/queue/auth
mounts (``:z`` on the podman path only, auth staged writable), ``--tmpfs
/tmp``, the five limit flags with defaults and env overrides,
``oom_score_adj``, image tag, cpuset-wider-than-machine fail-fast, missing
queue dir fail-fast, argument forwarding, and the bounded restart loop
(exit 137 / other / 0).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "harness-run"

# Coreutils the script shells out to; symlinked into the fake bin dir so the
# test PATH contains no real container engine.
REQUIRED_TOOLS = (
    "bash", "dirname", "mktemp", "cp", "rm", "tr", "sleep", "python3",
    "nproc", "cat",
)

FAKE_ENGINE = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_ENGINE_LOG"
count=$(( $(cat "$FAKE_ENGINE_COUNT") + 1 ))
echo "$count" > "$FAKE_ENGINE_COUNT"
exits=($FAKE_ENGINE_EXITS)
idx=$(( count - 1 ))
if [ "$idx" -lt "${#exits[@]}" ]; then
    exit "${exits[$idx]}"
fi
exit 0
"""


class _HarnessRunScriptBase(unittest.TestCase):
    """Temp dirs, sanitized PATH with fake engines, fake sysfs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.bindir = self.base / "bin"
        self.bindir.mkdir()
        for tool in REQUIRED_TOOLS:
            source = shutil.which(tool)
            self.assertIsNotNone(source, f"required tool missing: {tool}")
            os.symlink(source, self.bindir / tool)

        self.workdir = self.base / "work"
        self.queue = self.workdir / "queue"
        self.workdir.mkdir()
        self.queue.mkdir()
        self.pi_agent = self.base / "pi-agent"
        self.pi_agent.mkdir()
        (self.pi_agent / "auth.json").write_text("{}")

        self.sysfs = self.base / "sys"
        cpu_dir = self.sysfs / "devices" / "system" / "cpu"
        cpu_dir.mkdir(parents=True)
        (cpu_dir / "online").write_text("0-31\n")

        self.engine_log = self.base / "engine.log"
        self.engine_count = self.base / "engine.count"
        self.engine_count.write_text("0")

        self.home = self.base / "home"
        self.home.mkdir()

    def install_fake_engine(self, name: str) -> None:
        engine = self.bindir / name
        engine.write_text(FAKE_ENGINE)
        engine.chmod(0o755)

    def run_script(
        self,
        args=(),
        *,
        dry_run: bool = True,
        env_overrides=None,
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": str(self.bindir),
            "HOME": str(self.home),
            "FAKE_ENGINE_LOG": str(self.engine_log),
            "FAKE_ENGINE_COUNT": str(self.engine_count),
            "FAKE_ENGINE_EXITS": "",
            "WORK_DIR": str(self.workdir),
            "HARNESS_QUEUE_DIR": str(self.queue),
            "PI_AGENT_DIR": str(self.pi_agent),
            "HARNESS_SYSFS_DIR": str(self.sysfs),
            "HARNESS_RESTART_SLEEP": "0",
        }
        if dry_run:
            env["HARNESS_DRY_RUN"] = "1"
        env.update(env_overrides or {})
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def engine_invocations(self) -> list:
        if not self.engine_log.exists():
            return []
        return self.engine_log.read_text().splitlines()


class TestScriptSurface(_HarnessRunScriptBase):
    def test_executable_bit_set(self) -> None:
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_bash_syntax_clean(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uses_set_u_without_set_e(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn("set -u", text)
        self.assertNotIn("set -e", text)


class TestDryRunPodman(_HarnessRunScriptBase):
    def setUp(self) -> None:
        super().setUp()
        self.install_fake_engine("podman")
        self.result = self.run_script()

    def test_dry_run_exits_zero_without_invoking_engine(self) -> None:
        self.assertEqual(self.result.returncode, 0, self.result.stderr)
        self.assertEqual(self.engine_invocations(), [])

    def test_workdir_mounted_at_own_path_and_workspace_with_selinux(self) -> None:
        out = self.result.stdout
        self.assertIn(f"-v {self.workdir}:{self.workdir}:z", out)
        self.assertIn(f"-v {self.workdir}:/workspace:z", out)

    def test_queue_mounted_at_same_path(self) -> None:
        self.assertIn(f"-v {self.queue}:{self.queue}:z", self.result.stdout)

    def test_pi_auth_staged_writable(self) -> None:
        out = self.result.stdout
        self.assertIn(":/home/harnessuser/.pi/agent", out)
        # Staged copy, not the host dir itself, and never read-only.
        self.assertNotIn(f"{self.pi_agent}:/home/harnessuser/.pi/agent", out)
        self.assertNotIn(":ro", out)

    def test_tmp_is_tmpfs(self) -> None:
        self.assertIn("--tmpfs /tmp", self.result.stdout)

    def test_default_resource_limits(self) -> None:
        out = self.result.stdout
        self.assertIn("--memory 4g", out)
        self.assertIn("--memory-swap 4g", out)
        self.assertIn("--cpuset-cpus 8-15", out)
        self.assertIn("--cpus 8.0", out)
        self.assertIn("--pids-limit 300", out)

    def test_oom_score_adj_sacrifice(self) -> None:
        self.assertIn("--oom-score-adj 500", self.result.stdout)

    def test_default_image_tag(self) -> None:
        self.assertIn("harness-sandbox:frozen-latest", self.result.stdout)

    def test_invokes_harness_continue(self) -> None:
        self.assertIn("python3 harness.py run --continue", self.result.stdout)


class TestDryRunOverrides(_HarnessRunScriptBase):
    def setUp(self) -> None:
        super().setUp()
        self.install_fake_engine("podman")

    def test_env_overrides_reflected_in_command(self) -> None:
        result = self.run_script(
            env_overrides={
                "HARNESS_MEM": "1g",
                "HARNESS_MEM_SWAP": "1g",
                "HARNESS_CPUSET": "0-3",
                "HARNESS_CPU_QUOTA": "2.5",
                "HARNESS_PIDS_LIMIT": "64",
                "IMAGE_TAG": "custom:tag",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("--memory 1g", out)
        self.assertIn("--memory-swap 1g", out)
        self.assertIn("--cpuset-cpus 0-3", out)
        self.assertIn("--cpus 2.5", out)
        self.assertIn("--pids-limit 64", out)
        self.assertIn("custom:tag", out)

    def test_extra_args_forwarded_after_continue(self) -> None:
        result = self.run_script(args=["--task", "foo"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "python3 harness.py run --continue --task foo", result.stdout
        )

    def test_work_dir_resolved_from_config_when_unset(self) -> None:
        config = self.base / "config.json"
        config.write_text(json.dumps({"workDir": str(self.workdir)}))
        overrides = {
            "HARNESS_CONFIG": str(config),
            "WORK_DIR": "",
            "HARNESS_QUEUE_DIR": "",
        }
        result = self.run_script(env_overrides=overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"-v {self.workdir}:{self.workdir}:z", result.stdout)
        self.assertIn(f"-v {self.queue}:{self.queue}:z", result.stdout)


class TestDockerEnginePath(_HarnessRunScriptBase):
    def setUp(self) -> None:
        super().setUp()
        self.install_fake_engine("podman")
        self.install_fake_engine("docker")

    def test_docker_path_omits_selinux_flags(self) -> None:
        result = self.run_script(env_overrides={"HARNESS_ENGINE": "docker"})
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("docker run --rm -i", out)
        self.assertNotIn(":z", out)
        self.assertIn(f"-v {self.workdir}:{self.workdir} ", out)

    def test_podman_preferred_when_both_present(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("podman run --rm -i", result.stdout)


class TestEngineDetectionFailures(_HarnessRunScriptBase):
    def test_missing_engine_exits_nonzero_with_message(self) -> None:
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("podman", result.stderr)
        self.assertIn("docker", result.stderr)

    def test_invalid_harness_engine_value_rejected(self) -> None:
        self.install_fake_engine("podman")
        result = self.run_script(env_overrides={"HARNESS_ENGINE": "containerd"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HARNESS_ENGINE", result.stderr)

    def test_harness_engine_naming_absent_engine_rejected(self) -> None:
        self.install_fake_engine("docker")
        result = self.run_script(env_overrides={"HARNESS_ENGINE": "podman"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("podman", result.stderr)


class TestCpusetFailFast(_HarnessRunScriptBase):
    def setUp(self) -> None:
        super().setUp()
        self.install_fake_engine("podman")

    def set_host_cpus(self, online: str) -> None:
        (self.sysfs / "devices" / "system" / "cpu" / "online").write_text(online)

    def test_cpuset_wider_than_host_fails_fast(self) -> None:
        self.set_host_cpus("0-7")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cpuset", result.stderr)
        self.assertIn("0-7", result.stderr)
        self.assertEqual(self.engine_invocations(), [])

    def test_cpuset_within_host_passes(self) -> None:
        self.set_host_cpus("0-7")
        result = self.run_script(env_overrides={"HARNESS_CPUSET": "0-7"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_comma_separated_cpuset_checked(self) -> None:
        self.set_host_cpus("0-7")
        result = self.run_script(env_overrides={"HARNESS_CPUSET": "0,2-15"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cpuset", result.stderr)


class TestAuthStagingEdgeCases(_HarnessRunScriptBase):
    def test_no_auth_mount_without_auth_json(self) -> None:
        self.install_fake_engine("podman")
        (self.pi_agent / "auth.json").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("/home/harnessuser/.pi/agent", result.stdout)


class TestQueueDirFailFast(_HarnessRunScriptBase):
    """Spec §9: a missing queue dir aborts the launch, never a silent mount."""

    def test_missing_queue_dir_exits_nonzero_before_launch(self) -> None:
        self.install_fake_engine("podman")
        result = self.run_script(
            dry_run=False,
            env_overrides={"HARNESS_QUEUE_DIR": str(self.base / "no-queue")},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("queue directory", result.stderr)
        self.assertEqual(self.engine_invocations(), [])

    def test_missing_queue_dir_fails_dry_run_too(self) -> None:
        """Dry-run is a config check too — a broken queue must not print a
        command that would fail moments later at launch."""
        self.install_fake_engine("podman")
        result = self.run_script(
            env_overrides={"HARNESS_QUEUE_DIR": str(self.base / "no-queue")},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("queue directory", result.stderr)

    def test_file_where_queue_dir_expected_is_rejected(self) -> None:
        self.install_fake_engine("podman")
        as_file = self.base / "queue-file"
        as_file.write_text("not a directory\n")
        result = self.run_script(
            env_overrides={"HARNESS_QUEUE_DIR": str(as_file)},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("queue directory", result.stderr)


class TestRestartLoop(_HarnessRunScriptBase):
    def test_137_then_crash_then_success_terminates_loop(self) -> None:
        self.install_fake_engine("podman")
        result = self.run_script(
            dry_run=False,
            env_overrides={"FAKE_ENGINE_EXITS": "137 3 0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.engine_invocations()), 3)
        out = result.stdout
        self.assertIn("OOM killer (exit 137)", out)
        self.assertIn("crashed with code 3", out)
        self.assertIn("completed successfully (exit 0)", out)

    def test_engine_receives_continue_command_each_iteration(self) -> None:
        self.install_fake_engine("podman")
        result = self.run_script(
            dry_run=False,
            env_overrides={"FAKE_ENGINE_EXITS": "137 0", "IMAGE_TAG": "img:1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in self.engine_invocations():
            self.assertIn("run --rm -i", line)
            self.assertIn("img:1 python3 harness.py run --continue", line)


if __name__ == "__main__":
    unittest.main()
