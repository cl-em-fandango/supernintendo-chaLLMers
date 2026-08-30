"""Container driver and ephemeral snapshot management for E2E testing."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_IMAGE_TAG = "harness-e2e-base:latest"
STARTPOINT_IMAGE_TAG = "harness-e2e-startpoint:latest"

# Default models matching the pre-configured e2e-test-agent
DEFAULT_TW_MODEL = "Qwen3.8-Flash-Next-UD-Q4_K_XL_TechnicalWriter"
DEFAULT_IMP_MODEL = "Qwen3.8-Flash-Next-UD-Q4_K_XL_Implementer"
DEFAULT_ASSESSOR_MODEL = "Ornith-1.5-35B-Q6_K"

_TRACKED_CONTAINERS: set[tuple[str, str]] = set()
_CONTAINER_LOCK = threading.Lock()


def register_container(engine: str, name: str) -> None:
    """Register an active container for automatic teardown on exit or interrupt."""
    with _CONTAINER_LOCK:
        _TRACKED_CONTAINERS.add((engine, name))


def unregister_container(engine: str, name: str) -> None:
    """Unregister a container that has been cleaned up."""
    with _CONTAINER_LOCK:
        _TRACKED_CONTAINERS.discard((engine, name))


def force_cleanup_container(engine: str, container_name: str) -> None:
    """Forcibly stop, kill, and remove a container without hanging in stopping state.

    First sends SIGKILL directly to avoid graceful-stop deadlocks (e.g. hung subshells),
    then force-removes the container and its ephemeral storage with a strict timeout.
    """
    try:
        # Step 1: Immediate SIGKILL to prevent stopping state hangs
        subprocess.run(
            [engine, "kill", container_name],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        pass

    try:
        # Step 2: Force remove container with volumes
        subprocess.run(
            [engine, "rm", "-f", "-v", container_name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception:
        pass


def cleanup_all_containers() -> None:
    """Reap all tracked and labeled E2E test containers."""
    with _CONTAINER_LOCK:
        containers = list(_TRACKED_CONTAINERS)
        _TRACKED_CONTAINERS.clear()

    for engine, name in containers:
        force_cleanup_container(engine, name)

    # Belt and braces: sweep any leftover containers carrying the harness-e2e label
    for engine in ("podman", "docker"):
        if shutil.which(engine):
            try:
                res = subprocess.run(
                    [engine, "ps", "-a", "--filter", "label=harness-e2e=true", "-q"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    c_ids = res.stdout.strip().split()
                    for cid in c_ids:
                        force_cleanup_container(engine, cid)
            except Exception:
                pass


# Automatic exit hook for unexpected process exits
atexit.register(cleanup_all_containers)


def get_container_engine() -> str:
    """Find podman or docker on PATH."""
    override = os.environ.get("CONTAINER_ENGINE")
    if override:
        if shutil.which(override):
            return override
        raise RuntimeError(f"Configured CONTAINER_ENGINE='{override}' not found on PATH.")
    if shutil.which("podman"):
        return "podman"
    if shutil.which("docker"):
        return "docker"
    raise RuntimeError("Neither 'podman' nor 'docker' container engine was found on PATH.")


@dataclass
class ContainerExecutionResult:
    returncode: int
    stdout: str
    stderr: str


class EphemeralContainer:
    """An isolated container instance spawned from the snapshot startpoint."""

    def __init__(
        self,
        engine: str,
        container_id: str,
        container_name: str,
        tw_model: str = DEFAULT_TW_MODEL,
        imp_model: str = DEFAULT_IMP_MODEL,
        assessor: str = DEFAULT_ASSESSOR_MODEL,
    ):
        self.engine = engine
        self.container_id = container_id
        self.container_name = container_name
        self.tw_model = tw_model
        self.imp_model = imp_model
        self.assessor = assessor
        self.workspace_dir = "/workspace"
        register_container(self.engine, self.container_name)

    def is_running(self) -> bool:
        """Check if the container is currently running."""
        res = subprocess.run(
            [self.engine, "inspect", "--format", "{{.State.Running}}", self.container_name],
            capture_output=True,
            text=True,
        )
        return res.returncode == 0 and res.stdout.strip().lower() == "true"

    def get_logs(self) -> str:
        """Retrieve container runtime stdout/stderr."""
        res = subprocess.run(
            [self.engine, "logs", self.container_name],
            capture_output=True,
            text=True,
        )
        return res.stdout + ("\n" + res.stderr if res.stderr else "")

    def exec(
        self,
        command: list[str] | str,
        *,
        workdir: str | None = None,
        check: bool = False,
        env: dict[str, str] | None = None,
        timeout: int = 3600,
        stream: bool = True,
    ) -> ContainerExecutionResult:
        """Execute a command inside the running container."""
        if not self.is_running():
            logs = self.get_logs()
            raise RuntimeError(
                f"Container '{self.container_name}' is not running. Logs:\n{logs}"
            )

        cmd = [self.engine, "exec"]
        if workdir:
            cmd.extend(["-w", workdir])

        default_env = {
            "PYTHONPATH": "/opt/harness-frozen",
            "HARNESS_CONFIG": "/workspace/config.json",
            "PI_CODING_AGENT_DIR": "/home/harnessuser/.pi/agent",
            "HOME": "/home/harnessuser",
            "HARNESS_PI_PROVIDER": os.environ.get("HARNESS_PI_PROVIDER", ""),
            "DISABLE_FIREWALL": "1",
            "PYTHONUNBUFFERED": "1",
        }
        if env:
            default_env.update(env)

        for k, v in default_env.items():
            cmd.extend(["-e", f"{k}={v}"])

        if isinstance(command, str):
            cmd.extend([self.container_name, "bash", "-c", command])
        else:
            cmd.append(self.container_name)
            cmd.extend(command)

        if not stream:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
            except (KeyboardInterrupt, BaseException) as exc:
                try:
                    subprocess.run([self.engine, "kill", self.container_name], capture_output=True, timeout=5, check=False)
                except Exception:
                    pass
                raise exc
        else:
            stdout_parts: list[str] = []
            stderr_parts: list[str] = []
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            assert proc.stderr is not None

            def read_stderr():
                for line in proc.stderr:
                    stderr_parts.append(line)
                    sys.stderr.write(line)
                    sys.stderr.flush()

            err_thread = threading.Thread(target=read_stderr, daemon=True)
            err_thread.start()

            try:
                for line in proc.stdout:
                    stdout_parts.append(line)
                    sys.stdout.write(line)
                    sys.stdout.flush()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except (KeyboardInterrupt, BaseException) as exc:
                try:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=2)
                except Exception:
                    pass
                try:
                    subprocess.run([self.engine, "kill", self.container_name], capture_output=True, timeout=5, check=False)
                except Exception:
                    pass
                raise exc
            finally:
                err_thread.join(timeout=2)

            rc = proc.returncode
            stdout = "".join(stdout_parts)
            stderr = "".join(stderr_parts)

        if check and rc != 0:
            raise RuntimeError(
                f"Container exec failed (rc={rc}):\n"
                f"Command: {command}\n"
                f"Stdout: {stdout}\n"
                f"Stderr: {stderr}"
            )
        return ContainerExecutionResult(rc, stdout, stderr)

    def write_file(self, container_path: str, content: str) -> None:
        """Write content to a file inside the container."""
        proc = subprocess.run(
            [self.engine, "exec", "-i", self.container_name, "bash", "-c", f"cat > '{container_path}'"],
            input=content,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to write to {container_path}: {proc.stderr}")

    def read_file(self, container_path: str) -> str:
        """Read a file from inside the container."""
        res = self.exec(["cat", container_path], check=True)
        return res.stdout

    def file_exists(self, container_path: str) -> bool:
        """Check if a file exists in the container."""
        res = self.exec(["test", "-e", container_path])
        return res.returncode == 0

    def write_config(
        self,
        tw_model: str | None = None,
        imp_model: str | None = None,
        assessor: str | None = None,
    ) -> None:
        """Write a clean, concrete config.json inside the container."""
        tw = tw_model or self.tw_model
        imp = imp_model or self.imp_model
        a = assessor or self.assessor
        cfg_dict = {
            "workDir": "/workspace",
            "logDir": "/workspace/logs",
            "statsDir": "/workspace/stats",
            "queueDir": "/workspace/queue",
            "tokenBudget": 200000,
            "maxSpecKickbacks": 3,
            "maxSliceImplement": 5,
            "maxSliceTechReview": 5,
            "maxSliceFuncReview": 5,
            "maxSliceCheckLoops": 3,
            "autonomousQueueTarget": 5,
            "trunkBranch": "pi/trunk",
            "taskProvider": "directory",
            "directoryProvider": {
                "pendingDir": "/workspace/queue/pending",
                "claimedDir": "/workspace/queue/claimed",
            },
            "models": {
                "technicalWriter": tw,
                "implementer": imp,
                "assessor": a,
            },
        }
        self.write_file(f"{self.workspace_dir}/config.json", json.dumps(cfg_dict, indent=2))
        self.write_file(f"{self.workspace_dir}/target_repo/config.json", json.dumps(cfg_dict, indent=2))

    def write_task(self, task_id: str, title: str, requirements: str) -> None:
        """Create a new task in /workspace/queue/pending/<task_id>.md."""
        target_repo = f"{self.workspace_dir}/target_repo"
        content = f"""# {title}

Target repository: {target_repo}

## Requirements
{requirements}
"""
        self.write_file(f"{self.workspace_dir}/queue/pending/{task_id}.md", content)

    def run_harness(self, *args: str, check: bool = False, timeout: int = 3600) -> ContainerExecutionResult:
        """Invoke harness.py inside the container."""
        cmd = ["python3", "/opt/harness-frozen/harness.py", *args]
        return self.exec(cmd, workdir=self.workspace_dir, check=check, timeout=timeout)

    def get_diagnostic_dump(self, task_id: str) -> str:
        """Collect diagnostic logs and status for troubleshooting a failed test."""
        dump_parts = []
        if self.file_exists("/workspace/logs/harness.log"):
            dump_parts.append("=== /workspace/logs/harness.log ===")
            dump_parts.append(self.read_file("/workspace/logs/harness.log"))

        review_path = f"/workspace/queue/review/{task_id}.md"
        if self.file_exists(review_path):
            dump_parts.append(f"=== {review_path} ===")
            dump_parts.append(self.read_file(review_path))

        status_res = self.run_harness("status")
        dump_parts.append("=== harness.py status ===")
        dump_parts.append(status_res.stdout)

        return "\n".join(dump_parts)

    def destroy(self) -> None:
        """Stop and remove the container instance to revert to clean state."""
        unregister_container(self.engine, self.container_name)
        force_cleanup_container(self.engine, self.container_name)


class ContainerLifecycleManager:
    """Manages building the base container, creating workspace snapshot, and spawning ephemeral instances."""

    def __init__(self, engine: str | None = None):
        self.engine = engine or get_container_engine()

    def build_base_image(self, image_tag: str = BASE_IMAGE_TAG) -> None:
        """Step 1: Build base container image with current source."""
        dockerfile_path = REPO_ROOT / "docker" / "Dockerfile"
        if not dockerfile_path.exists():
            raise FileNotFoundError(f"Dockerfile not found at {dockerfile_path}")

        print(f"==> [E2E] Building base container image '{image_tag}' via {self.engine}...")
        proc = subprocess.run(
            [
                self.engine, "build",
                "-t", image_tag,
                "-f", str(dockerfile_path),
                str(REPO_ROOT),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to build base container image {image_tag}:\n"
                f"STDOUT:\n{proc.stdout}\n"
                f"STDERR:\n{proc.stderr}"
            )

    def create_snapshot_startpoint(
        self,
        base_tag: str = BASE_IMAGE_TAG,
        snapshot_tag: str = STARTPOINT_IMAGE_TAG,
    ) -> None:
        """Step 2 & 3: Create folder structure in container, copy agent config, and save snapshot startpoint."""
        temp_builder_name = f"harness-builder-{uuid.uuid4().hex[:8]}"
        print(f"==> [E2E] Creating ephemeral workspace structure in builder container '{temp_builder_name}'...")

        # Spawn temporary builder container with host network
        proc = subprocess.run(
            [
                self.engine, "run", "-d",
                "--network", "host",
                "--label", "harness-e2e=true",
                "--name", temp_builder_name,
                "--entrypoint", "/bin/sh",
                base_tag,
                "-c", "tail -f /dev/null",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to start builder container: {proc.stderr}")
        register_container(self.engine, temp_builder_name)

        try:
            init_script = """#!/usr/bin/env bash
set -euo pipefail
WORKSPACE="/workspace"
mkdir -p "${WORKSPACE}"/{queue/pending,queue/claimed,queue/active,queue/done,queue/parked,queue/review,queue/failed,logs,stats,target_repo}
mkdir -p /home/harnessuser/.pi/agent /root/.pi/agent

# Copy e2e agent config into ~/.pi/agent for both users
if [ -d /opt/harness-frozen/e2e/e2e-test-agent ]; then
  cp -r /opt/harness-frozen/e2e/e2e-test-agent/* /home/harnessuser/.pi/agent/
  cp -r /opt/harness-frozen/e2e/e2e-test-agent/* /root/.pi/agent/
fi

# Configure global git safety for in-container executions
git config --global user.name "E2E Tester"
git config --global user.email "tester@harness.local"
git config --global init.defaultBranch "pi/trunk"
git config --global --add safe.directory "*"

# Initialize target_repo as a valid git repository with complete working copy of the codebase
rm -rf "${WORKSPACE}/target_repo"
mkdir -p "${WORKSPACE}/target_repo"

# Copy the entire codebase from /opt/harness-frozen to target_repo (including .gitignore, pyproject.toml, tests, etc.)
cp -a /opt/harness-frozen/. "${WORKSPACE}/target_repo/"

# Clean up any transient/irrelevant files from the working copy
rm -rf "${WORKSPACE}/target_repo/.git" "${WORKSPACE}/target_repo/.pi-*" "${WORKSPACE}/target_repo/logs" "${WORKSPACE}/target_repo/stats" "${WORKSPACE}/target_repo/queue"

# Initialize clean git repository on pi/trunk
cd "${WORKSPACE}/target_repo"
git init -b pi/trunk
git config user.name "E2E Tester"
git config user.email "tester@harness.local"
git add -A
git commit -m "chore: initial sandbox project setup"
git tag -f pi/last-good

chmod -R 777 "${WORKSPACE}" /home/harnessuser /root/.pi /opt/harness-frozen
"""
            exec_res = subprocess.run(
                [self.engine, "exec", "-i", temp_builder_name, "bash", "-c", init_script],
                capture_output=True,
                text=True,
            )
            if exec_res.returncode != 0:
                raise RuntimeError(
                    f"Workspace initialization inside builder failed:\n"
                    f"Stdout: {exec_res.stdout}\nStderr: {exec_res.stderr}"
                )

            # Step 3: Save container snapshot as start point
            print(f"==> [E2E] Saving container snapshot as '{snapshot_tag}'...")
            commit_res = subprocess.run(
                [self.engine, "commit", temp_builder_name, snapshot_tag],
                capture_output=True,
                text=True,
            )
            if commit_res.returncode != 0:
                raise RuntimeError(f"Failed to commit container snapshot: {commit_res.stderr}")
            print(f"==> [E2E] Container snapshot '{snapshot_tag}' committed successfully.")

        finally:
            unregister_container(self.engine, temp_builder_name)
            force_cleanup_container(self.engine, temp_builder_name)

    def spawn_ephemeral_container(
        self,
        snapshot_tag: str = STARTPOINT_IMAGE_TAG,
        tw_model: str = DEFAULT_TW_MODEL,
        imp_model: str = DEFAULT_IMP_MODEL,
        assessor: str = DEFAULT_ASSESSOR_MODEL,
        provider: str = "",
    ) -> EphemeralContainer:
        """Step 4: Spawn fresh ephemeral container instance from the snapshot startpoint."""
        container_name = f"harness-e2e-{uuid.uuid4().hex[:8]}"
        print(f"==> [E2E] Spawning ephemeral container '{container_name}' from snapshot '{snapshot_tag}'...")

        flags = [
            "-d",
            "--name", container_name,
            "--label", "harness-e2e=true",
            "-e", f"HARNESS_PI_PROVIDER={provider}",
            "-e", f"PI_E2E_TW_MODEL={tw_model}",
            "-e", f"PI_E2E_IMP_MODEL={imp_model}",
            "-e", f"PI_E2E_ASSESSOR={assessor}",
            "-e", "HARNESS_CONFIG=/workspace/config.json",
            "-e", "PYTHONPATH=/opt/harness-frozen",
            "-e", "PI_CODING_AGENT_DIR=/home/harnessuser/.pi/agent",
            "-e", "HOME=/home/harnessuser",
            "-e", "DISABLE_FIREWALL=1",
        ]

        # Networking configuration: default to host networking for direct access to LAN/host model servers
        net_mode = os.environ.get("CONTAINER_NETWORK", "host")
        if net_mode:
            flags.extend(["--network", net_mode])
        if net_mode != "host":
            if self.engine == "podman":
                flags.extend([
                    "--add-host=host.containers.internal:host-gateway",
                ])
            else:
                flags.extend([
                    "--add-host=host.docker.internal:host-gateway",
                ])

        # Override entrypoint to ensure reliable long-running daemon
        cmd = [
            self.engine, "run",
            *flags,
            "--entrypoint", "/bin/sh",
            snapshot_tag,
            "-c", "tail -f /dev/null",
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to spawn ephemeral container:\n"
                f"Command: {' '.join(cmd)}\n"
                f"Error: {proc.stderr}\n"
                f"Output: {proc.stdout}"
            )

        container_id = proc.stdout.strip()
        container = EphemeralContainer(
            self.engine,
            container_id,
            container_name,
            tw_model=tw_model,
            imp_model=imp_model,
            assessor=assessor,
        )

        # Wait up to 5 seconds to confirm container is healthy and running
        running = False
        for _ in range(10):
            if container.is_running():
                running = True
                break
            time.sleep(0.5)

        if not running:
            logs = container.get_logs()
            container.destroy()
            raise RuntimeError(
                f"Container '{container_name}' failed to stay running after start.\n"
                f"Container logs:\n{logs}"
            )

        # Write concrete config.json inside the running container with resolved models
        container.write_config(tw_model=tw_model, imp_model=imp_model, assessor=assessor)

        return container
