"""Containerized end-to-end integration tests that invoke real pi sessions."""
from __future__ import annotations

import json
import pytest

from .container_driver import EphemeralContainer


@pytest.mark.e2e
def test_e2e_container_implement_math_feature(ephemeral_container: EphemeralContainer):
    """Run a real pi session inside an ephemeral container to implement math utilities.

    Workflow executed in container:
    1. Writes task card into /workspace/queue/pending/001-math-tools.md.
    2. Runs `harness.py run-one` in the container, invoking real `pi` sessions through:
       spec -> assessment -> feasibility -> slicing -> implementation -> reviews -> merge.
    3. Verifies task reaches done/, review summary is created, and git squash-merge lands on pi/trunk.
    4. Runs `python3 -m unittest` on the target repo inside the container.
    5. After test finishes, fixture tears down the container, reverting to the pristine snapshot.
    """
    c = ephemeral_container
    task_id = "001-math-tools"

    requirements = """
Implement a pure Python utility file `math_tools.py` in the root of the target repository:
1. Function `power(base: float, exponent: int) -> float`: calculates base raised to exponent.
2. Function `is_even(n: int) -> bool`: returns True if n is an even integer, False otherwise.
3. Provide comprehensive unit tests in `tests/test_math_tools.py` using Python's standard `unittest`.
"""
    c.write_task(task_id, "Implement Math Tools (power and is_even)", requirements)

    # Execute harness run-one in container
    res = c.run_harness("run-one", timeout=1800)
    assert res.returncode == 0, f"Harness run-one failed with rc={res.returncode}:\nStdout: {res.stdout}\nStderr: {res.stderr}"

    # Verify task state in container
    assert not c.file_exists(f"/workspace/queue/active/{task_id}")
    assert c.file_exists(f"/workspace/queue/done/{task_id}")
    assert c.file_exists(f"/workspace/queue/review/{task_id}.md")

    # Verify task.json contents
    task_json_raw = c.read_file(f"/workspace/queue/done/{task_id}/task.json")
    task_state = json.loads(task_json_raw)
    assert task_state.get("status") == "done"
    assert "merge" in task_state.get("checkpointed_stages", [])

    # Verify review summary
    summary = c.read_file(f"/workspace/queue/review/{task_id}.md")
    assert "Status: DONE" in summary
    assert "Feature complete and merged to pi/trunk" in summary

    # Verify generated files on trunk in target_repo
    assert c.file_exists("/workspace/target_repo/math_tools.py")
    assert c.file_exists("/workspace/target_repo/tests/test_math_tools.py")

    # Run the generated tests inside the target repository in the container
    test_res = c.exec(
        ["python3", "-m", "unittest", "discover", "-s", "tests"],
        workdir="/workspace/target_repo",
    )
    assert test_res.returncode == 0, f"Target repo tests failed:\n{test_res.stdout}\n{test_res.stderr}"

    # Verify stats rows
    stats_raw = c.read_file("/workspace/stats/sessions.jsonl")
    lines = [json.loads(l) for l in stats_raw.splitlines() if l.strip()]
    assert len(lines) >= 5, f"Expected at least 5 sessions recorded, got {len(lines)}"


@pytest.mark.e2e
def test_e2e_container_implement_slugifier_feature(ephemeral_container: EphemeralContainer):
    """Run a real pi session inside an ephemeral container to implement string slugification."""
    c = ephemeral_container
    task_id = "002-slugify-util"

    requirements = """
Implement a string slugification helper `slug_utils.py` in the root of the target repository:
1. Function `slugify(text: str) -> str`: converts text to lowercase, replaces spaces and
   consecutive non-alphanumeric characters with a single hyphen, and strips leading/trailing hyphens.
2. Provide unit tests in `tests/test_slug_utils.py` verifying standard phrases, empty strings,
   and special characters.
"""
    c.write_task(task_id, "Implement String Slugifier", requirements)

    res = c.run_harness("run-one", timeout=1800)
    assert res.returncode == 0, f"Harness run-one failed with rc={res.returncode}:\nStdout: {res.stdout}\nStderr: {res.stderr}"

    assert c.file_exists(f"/workspace/queue/done/{task_id}")
    assert c.file_exists("/workspace/target_repo/slug_utils.py")
    assert c.file_exists("/workspace/target_repo/tests/test_slug_utils.py")

    test_res = c.exec(
        ["python3", "-m", "unittest", "discover", "-s", "tests"],
        workdir="/workspace/target_repo",
    )
    assert test_res.returncode == 0, f"Target repo tests failed:\n{test_res.stdout}\n{test_res.stderr}"
