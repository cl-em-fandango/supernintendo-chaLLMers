"""Slice 1 tests: checkpoint state in task.json (spec FR1, EC2/EC3/EC4/EC10/EC13).

Run from the repo root:  python3 -m unittest tests.test_checkpoint_state
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import CheckpointStage
from harness.core.providers import Task
from harness.workflow.task_lifecycle import TaskLifecycle, TaskState, write_atomic


def _cfg(queue_dir: Path) -> Config:
    return Config(
        harness_execution_and_queue_dir=queue_dir.parent,
        token_budget=100_000,
        max_spec_kickbacks=3,
        max_slice_implement=5,
        max_slice_tech_review=5,
        max_slice_func_review=5,
        max_slice_check_loops=3,
        autonomous_queue_target=5,
        trunk_branch="pi/trunk",
        task_provider="directory",
        directory_provider={},
        models={"technicalWriter": "m", "implementer": "m", "assessor": "m"},
        model_context_map={},
    )


class TaskLifecycleCheckpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue_dir = Path(self._tmp.name) / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.lifecycle = TaskLifecycle(_cfg(self.queue_dir), log=lambda *a: None)

    def _task(self, task_id="t1", body="# t1\n\nbody", source="directory:t1.md"):
        return Task(id=task_id, body=body, source=source)

    # ------------------------------------------------------------------
    # intake (F1.5)
    # ------------------------------------------------------------------
    def test_intake_writes_new_fields(self):
        td = self.lifecycle.intake(self._task())
        raw = json.loads((td / "task.json").read_text())
        self.assertEqual(raw["id"], "t1")
        self.assertEqual(raw["status"], "active")
        self.assertEqual(raw["source"], "directory:t1.md")
        self.assertEqual(raw["stage"], "spec")
        self.assertEqual(raw["history"], [])
        self.assertEqual(raw["checkpointed_stages"], [])
        self.assertEqual(raw["last_updated"], raw["created"])
        self.assertTrue((td / "original.md").exists())

    # ------------------------------------------------------------------
    # atomic writes (F1.2)
    # ------------------------------------------------------------------
    def test_write_atomic_replaces_file(self):
        path = self.queue_dir / "active" / "t1" / "task.json"
        path.parent.mkdir(parents=True)
        write_atomic(path, '{"a": 1}')
        write_atomic(path, '{"a": 2}')
        self.assertEqual(json.loads(path.read_text()), {"a": 2})
        self.assertFalse(path.with_name("task.json.tmp").exists())

    # ------------------------------------------------------------------
    # load_state tolerance (F1.4, EC2, EC3, EC4)
    # ------------------------------------------------------------------
    def _write_task_json(self, raw, where="active", task_id="t1"):
        path = self.queue_dir / where / task_id / "task.json"
        path.parent.mkdir(parents=True)
        path.write_text(raw if isinstance(raw, str) else json.dumps(raw))
        return path

    def test_load_old_format_treated_as_no_checkpoints(self):
        self._write_task_json({
            "id": "t1", "status": "active", "source": "s",
            "created": "2026-01-01T00:00:00+00:00", "stage": "spec", "history": [],
        })
        state = self.lifecycle.load_state("t1")
        self.assertEqual(state.checkpointed_stages, [])
        self.assertEqual(state.last_updated, "")

    def test_load_corrupt_json_treated_as_no_checkpoints(self):
        self._write_task_json("{not json")
        state = self.lifecycle.load_state("t1")
        self.assertEqual(state.checkpointed_stages, [])
        self.assertEqual(state.id, "t1")

    def test_load_unknown_stage_names_ignored(self):
        self._write_task_json({
            "id": "t1", "status": "active", "source": "s",
            "created": "c", "stage": "spec", "history": [],
            "checkpointed_stages": ["spec", "bogus", "holistic"],
            "last_updated": "c",
        })
        state = self.lifecycle.load_state("t1")
        self.assertEqual(state.checkpointed_stages, [CheckpointStage.SPEC])

    def test_load_dedupes_entries(self):
        self._write_task_json({
            "id": "t1", "status": "active", "source": "s",
            "created": "c", "stage": "spec", "history": [],
            "checkpointed_stages": ["spec", "spec", "feasibility"],
            "last_updated": "c",
        })
        state = self.lifecycle.load_state("t1")
        self.assertEqual(state.checkpointed_stages,
                         [CheckpointStage.SPEC, CheckpointStage.FEASIBILITY])

    # ------------------------------------------------------------------
    # checkpoint / set_stage (F1.1, F1.3, EC10)
    # ------------------------------------------------------------------
    def _intake(self, task_id="t1", body="# t1\n\nbody"):
        return self.lifecycle.intake(self._task(task_id, body=body))

    def test_checkpoint_appends_in_order(self):
        self._intake()
        self.lifecycle.checkpoint("t1", CheckpointStage.SPEC)
        self.lifecycle.checkpoint("t1", CheckpointStage.FEASIBILITY)
        raw = json.loads(self.lifecycle.task_json_path("t1").read_text())
        self.assertEqual(raw["checkpointed_stages"], ["spec", "feasibility"])

    def test_checkpoint_is_idempotent(self):
        self._intake()
        self.lifecycle.checkpoint("t1", CheckpointStage.SPEC)
        self.lifecycle.checkpoint("t1", CheckpointStage.FEASIBILITY)
        # feasibility kickback re-completes spec (EC10): list must stay a prefix
        self.lifecycle.checkpoint("t1", CheckpointStage.SPEC)
        raw = json.loads(self.lifecycle.task_json_path("t1").read_text())
        self.assertEqual(raw["checkpointed_stages"], ["spec", "feasibility"])

    def test_checkpoint_bumps_last_updated(self):
        self._intake()
        created = json.loads(self.lifecycle.task_json_path("t1").read_text())["created"]
        self.lifecycle.checkpoint("t1", CheckpointStage.SPEC)
        raw = json.loads(self.lifecycle.task_json_path("t1").read_text())
        self.assertGreaterEqual(raw["last_updated"], created)

    def test_set_stage_updates_stage_field(self):
        self._intake()
        self.lifecycle.set_stage("t1", CheckpointStage.SLICING)
        raw = json.loads(self.lifecycle.task_json_path("t1").read_text())
        self.assertEqual(raw["stage"], "slicing")

    def test_state_roundtrip_via_where(self):
        self._intake()
        self.lifecycle.checkpoint("t1", CheckpointStage.SPEC)
        # simulate a parked dir: move it, then read with where="parked"
        import shutil
        shutil.move(str(self.queue_dir / "active" / "t1"),
                    str(self.queue_dir / "parked" / "t1"))
        state = self.lifecycle.load_state("t1", where="parked")
        self.assertEqual(state.checkpointed_stages, [CheckpointStage.SPEC])
        self.lifecycle.checkpoint("t1", CheckpointStage.FEASIBILITY, where="parked")
        state = self.lifecycle.load_state("t1", where="parked")
        self.assertEqual(state.checkpointed_stages,
                         [CheckpointStage.SPEC, CheckpointStage.FEASIBILITY])

    # ------------------------------------------------------------------
    # resolve_workdir guard (EC13)
    # ------------------------------------------------------------------
    def test_resolve_workdir_without_config_falls_back(self):
        td = self._intake()
        (td / "original.md").unlink()
        self.assertEqual(self.lifecycle.resolve_workdir(td), td)

    def test_resolve_workdir_uses_config_repo_dir(self):
        repo = self.queue_dir / "repo"
        (repo / ".git").mkdir(parents=True)
        self.lifecycle.cfg.target_codebase_dir = repo
        td = self._intake(body="# t1\n\nbody")
        self.assertEqual(self.lifecycle.resolve_workdir(td), repo)


if __name__ == "__main__":
    unittest.main()
