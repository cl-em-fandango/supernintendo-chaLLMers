"""Container driver and ephemeral snapshot management for E2E testing."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_IMAGE_TAG = "harness-e2e-base:latest"
STARTPOINT_IMAGE_TAG = "harness-e2e-startpoint:latest"


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

    def __init__(self, engine: str, container_id: str, container_name: str):
        self.engine = engine
        self.container_id = container_id
        self.container_name = container_name
        self.workspace_dir = "/workspace"

    def exec(
        self,
        command: list[str] | str,
        *,
        workdir: str | None = None,
        check: bool = False,
        env: dict[str, str] | None = None,
        timeout: int = 1800,
    ) -> ContainerExecutionResult:
        """Execute a command inside the running container."""
        cmd = [self.engine, "exec"]
        if workdir:
            cmd.extend(["-w", workdir])
        if env:
            for k, v in env.items():
                cmd.extend(["-e", f"{k}={v}"])

        if isinstance(command, str):
            cmd.extend([self.container_name, "bash", "-c", command])
        else:
            cmd.append(self.container_name)
            cmd.extend(command)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"Container exec failed (rc={proc.returncode}):\n"
                f"Command: {command}\n"
                f"Stdout: {proc.stdout}\n"
                f"Stderr: {proc.stderr}"
            )
        return ContainerExecutionResult(proc.returncode, proc.stdout, proc.stderr)

    def write_file(self, container_path: str, content: str) -> None:
        """Write content to a file inside the container."""
        proc = subprocess.run(
            [self.engine, "exec", "-i", self.container_name, "bash", "-c", f"cat > '{container_path}'"],
            input=content,
            text=True,
            capture_output=True,
            check=True,
        )

    def read_file(self, container_path: str) -> str:
        """Read a file from inside the container."""
        res = self.exec(["cat", container_path], check=True)
        return res.stdout

    def file_exists(self, container_path: str) -> bool:
        """Check if a file exists in the container."""
        res = self.exec(["test", "-e", container_path])
        return res.returncode == 0

    def write_task(self, task_id: str, title: str, requirements: str) -> None:
        """Create a new task in /workspace/queue/pending/<task_id>.md."""
        target_repo = f"{self.workspace_dir}/target_repo"
        content = f"""# {title}

Target repository: {target_repo}

## Requirements
{requirements}
"""
        self.write_file(f"{self.workspace_dir}/queue/pending/{task_id}.md", content)

    def run_harness(self, *args: str, check: bool = False, timeout: int = 1800) -> ContainerExecutionResult:
        """Invoke harness.py inside the container."""
        cmd = ["python3", "/opt/harness-frozen/harness.py", *args]
        return self.exec(cmd, workdir=self.workspace_dir, check=check, timeout=timeout)

    def destroy(self) -> None:
        """Stop and remove the container instance to revert to clean state."""
        subprocess.run(
            [self.engine, "rm", "-f", self.container_name],
            capture_output=True,
            check=False,
        )


class ContainerLifecycleManager:
    """Manages building the base container, creating workspace snapshot, and spawning ephemeral instances."""

    def __init__(self, engine: str | None = None):
        self.engine = engine or get_container_engine()

    def build_base_image(self, image_tag: str = BASE_IMAGE_TAG) -> None:
        """Build the base container image with current codebase version."""
        dockerfile_path = REPO_ROOT / "docker" / "Dockerfile"
        if not dockerfile_path.exists():
            raise FileNotFoundError(f"Dockerfile not found at {dockerfile_path}")

        print(f"==> [E2E] Building base container image '{image_tag}' via {self.engine}...")
        subprocess.run(
            [
                self.engine, "build",
                "-t", image_tag,
                "-f", str(dockerfile_path),
                str(REPO_ROOT),
            ],
            check=True,
        )

    def create_snapshot_startpoint(
        self,
        base_tag: str = BASE_IMAGE_TAG,
        snapshot_tag: str = STARTPOINT_IMAGE_TAG,
    ) -> None:
        """Initialize folder structure inside a container and commit as snapshot start point."""
        temp_builder_name = f"harness-builder-{uuid.uuid4().hex[:8]}"
        print(f"==> [E2E] Creating ephemeral workspace structure in builder container '{temp_builder_name}'...")

        # Run temporary container
        subprocess.run(
            [
                self.engine, "run", "-d",
                "--name", temp_builder_name,
                base_tag,
                "sleep", "300",
            ],
            check=True,
            capture_output=True,
        )

        try:
            # Script to initialize folder structure and mock target repo
            init_script = """#!/usr/bin/env bash
set -euo pipefail
WORKSPACE="/workspace"
mkdir -p "${WORKSPACE}"/{queue/pending,queue/claimed,queue/active,queue/done,queue/parked,queue/review,queue/failed,logs,stats,target_repo}

# Configure config.json
cat << 'EOF' > "${WORKSPACE}/config.json"
{
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
    "claimedDir": "/workspace/queue/claimed"
  },
  "models": {
    "technicalWriter": "${PI_E2E_MODEL:-qwen2.5-coder:14b}",
    "implementer": "${PI_E2E_MODEL:-qwen2.5-coder:14b}",
    "assessor": "${PI_E2E_ASSESSOR:-qwen2.5-coder:14b}"
  }
}
EOF

# Initialize target_repo as a valid git repository with harness skeleton for gate checks
cd "${WORKSPACE}/target_repo"
git init -b pi/trunk
git config user.name "E2E Tester"
git config user.email "tester@harness.local"

cp -r /opt/harness-frozen/harness ./harness
cp -r /opt/harness-frozen/external ./external
cp /opt/harness-frozen/harness.py ./harness.py
cp "${WORKSPACE}/config.json" ./config.json
echo "# Target Project" > README.md
if [ -f /opt/harness-frozen/pyproject.toml ]; then
  cp /opt/harness-frozen/pyproject.toml ./pyproject.toml
fi

git add -A
git commit -m "chore: initial sandbox project setup"
git tag -f pi/last-good

chown -R harnessuser:harnessuser "${WORKSPACE}"
"""
            subprocess.run(
                [self.engine, "exec", "-i", temp_builder_name, "bash", "-c", init_script],
                check=True,
                capture_output=True,
            )

            # Commit the configured container as the snapshot startpoint
            print(f"==> [E2E] Saving container snapshot as '{snapshot_tag}'...")
            subprocess.run(
                [self.engine, "commit", temp_builder_name, snapshot_tag],
                check=True,
                capture_output=True,
            )
        finally:
            subprocess.run([self.engine, "rm", "-f", temp_builder_name], check=False, capture_output=True)

    def spawn_ephemeral_container(
        self,
        snapshot_tag: str = STARTPOINT_IMAGE_TAG,
        model: str = "qwen2.5-coder:14b",
        assessor: str = "qwen2.5-coder:14b",
        provider: str = "llama-swap",
    ) -> EphemeralContainer:
        """Spawn a fresh ephemeral container instance from the startpoint snapshot."""
        container_name = f"harness-e2e-{uuid.uuid4().hex[:8]}"

        flags = [
            "-d",
            "--name", container_name,
            "--cap-add=NET_ADMIN",
            "--cap-add=NET_RAW",
            "-e", f"HARNESS_PI_PROVIDER={provider}",
            "-e", f"PI_E2E_MODEL={model}",
            "-e", f"PI_E2E_ASSESSOR={assessor}",
        ]

        if self.engine == "podman":
            flags.extend([
                "--userns=keep-id",
                "--add-host=host.containers.internal:host-gateway",
            ])
        else:
            flags.extend([
                "--add-host=host.docker.internal:host-gateway",
            ])

        # Stage writable host ~/.pi credentials if present
        host_pi_dir = Path.home() / ".pi" / "agent"
        if host_pi_dir.exists() and (host_pi_dir / "auth.json").exists():
            flags.extend(["-v", f"{host_pi_dir}:/home/harnessuser/.pi/agent:ro,z"])

        cmd = [self.engine, "run", *flags, snapshot_tag, "sleep", "3600"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        container_id = proc.stdout.strip()

        return EphemeralContainer(self.engine, container_id, container_name)
