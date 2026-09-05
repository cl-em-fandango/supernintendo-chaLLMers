"""Slice 3 — demo flag propagation: sidecar -> `Task.meta` -> `task.json`.

Covers demo spec FR-1.4 (the FR-1 <-> FR-6 bridge):

  * claiming a pending task whose linkage sidecar carries `demo: true`
    yields `Task.meta["demo"] is True`; an unflagged (or unlinked, or
    corrupt-sidecar) task yields an absent/False flag;
  * the flag is visible whether the sidecar sits beside the claimed file or
    was left behind in `pending/` when the claim renamed the markdown;
  * `TaskLifecycle.intake` persists the flag as `"demo": true` in
    `task.json`, and a reload after a simulated resume returns the same
    value; every later `task.json` rewrite (checkpoint, stage stamp,
    terminal status stamp) keeps it;
  * a legacy `task.json` without the field loads with `demo=False` and does
    not crash.

All tests run in-process against temp queue directories — no network, no
git, no `pi` (spec §6).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core.config import Config
from harness.core.enums import CheckpointStage
from harness.core.providers import DirectoryTaskProvider, Task
from harness.core import task_record
from tests.legacy_sidecars import (
    SyncLinkage,
    file_sidecar_path,
    write_legacy_linkage,
)
from harness.workflow.task_lifecycle import TaskLifecycle

REPO = "acme/widgets"


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


class _QueueMixin:
    """A temp queue tree with `pending/` and `claimed/` task-file dirs."""

    def _make_queue(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue_dir = Path(self._tmp.name) / "queue"
        for sub in ("pending", "claimed", "active", "done", "failed",
                    "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.pending_dir = self.queue_dir / "pending"
        self.claimed_dir = self.queue_dir / "claimed"

    def _provider(self) -> DirectoryTaskProvider:
        return DirectoryTaskProvider(self.pending_dir, self.claimed_dir,
                                     log=lambda *a: None)

    def _lifecycle(self) -> TaskLifecycle:
        return TaskLifecycle(_cfg(self.queue_dir), log=lambda *a: None)

    def _pending_task(self, name: str = "demo_app", body: str = "# Demo App\n\nbody",
                      demo: bool | None = True) -> Path:
        """Write `pending/<name>.md` and, unless `demo is None`, its sidecar."""
        path = self.pending_dir / f"{name}.md"
        path.write_text(body)
        if demo is not None:
            write_legacy_linkage(file_sidecar_path(path),
                          SyncLinkage(issue=7, repo=REPO, demo=demo))
        return path


class ClaimMetaTest(_QueueMixin, unittest.TestCase):
    """`Task.meta["demo"]` comes off the linkage sidecar at claim time."""

    def setUp(self):
        self._make_queue()

    def test_claim_of_flagged_task_sets_meta_demo(self):
        self._pending_task()
        tasks = self._provider().fetch_pending(claim=True, owner="run-a")
        self.assertEqual(len(tasks), 1)
        self.assertIs(tasks[0].meta["demo"], True)

    def test_claim_of_unflagged_task_leaves_meta_empty(self):
        self._pending_task(demo=False)
        tasks = self._provider().fetch_pending(claim=True, owner="run-a")
        self.assertEqual(tasks[0].meta, {})
        self.assertIs(tasks[0].meta.get("demo", False), False)

    def test_task_without_sidecar_is_not_a_demo_task(self):
        self._pending_task(demo=None)
        tasks = self._provider().fetch_pending(claim=True, owner="run-a")
        self.assertIs(tasks[0].meta.get("demo", False), False)

    def test_corrupt_sidecar_is_not_a_demo_task(self):
        path = self._pending_task(demo=None)
        file_sidecar_path(path).write_text("{not json")
        tasks = self._provider().fetch_pending(claim=True, owner="run-a")
        self.assertIs(tasks[0].meta.get("demo", False), False)

    def test_flag_survives_the_claim_move(self):
        """The record is keyed by task id, so the rename cannot strand it.

        Both concerns resolve the record, so the claim write adopts the
        legacy linkage and retires the sidecar (FR-E2/FR-E3). The claimed
        task reads flagged in the fetch, the claim listing and the ownership
        listing, and the record holds the linkage and the ownership together.
        """
        sidecar = file_sidecar_path(self._pending_task())
        provider = self._provider()
        claimed = provider.fetch_pending(claim=True, owner="run-a")
        self.assertTrue((self.claimed_dir / "demo_app.md").exists())
        self.assertFalse(sidecar.exists(),
                         "a legacy linkage was left beside the claimed file")
        self.assertIs(claimed[0].meta["demo"], True)
        self.assertIs(provider.list_claims()[0].meta["demo"], True)
        self.assertIs(provider.list_owned_claims()[0].task.meta["demo"], True)
        record = task_record.read_record(self.queue_dir, "demo_app")
        self.assertIs(record.github.demo, True)
        self.assertEqual(record.claim.owner, "run-a")

    def test_sidecar_beside_the_claimed_file_is_read(self):
        """A sidecar written beside the claim (post-move) is honoured too."""
        self._pending_task(demo=None)
        provider = self._provider()
        claimed = provider.fetch_pending(claim=True, owner="run-a")
        self.assertIs(claimed[0].meta.get("demo", False), False)
        write_legacy_linkage(file_sidecar_path(self.claimed_dir / "demo_app.md"),
                      SyncLinkage(issue=7, repo=REPO, demo=True))
        self.assertIs(provider.list_claims()[0].meta["demo"], True)

    def test_fetch_without_claim_reads_the_flag(self):
        self._pending_task()
        tasks = self._provider().fetch_pending()
        self.assertIs(tasks[0].meta["demo"], True)

    def test_hand_built_task_has_no_demo_meta(self):
        """The Task default stays exactly what it was before the demo feature."""
        self.assertEqual(Task(id="t", body="b").meta, {})


class IntakePersistenceTest(_QueueMixin, unittest.TestCase):
    """`intake` persists the flag; `load_state` reads it back."""

    def setUp(self):
        self._make_queue()
        self.lifecycle = self._lifecycle()

    def _task_json(self, task_id: str = "demo_app", where: str = "active") -> dict:
        return json.loads(
            self.lifecycle.task_json_path(task_id, where).read_text())

    def test_intake_writes_demo_true(self):
        self.lifecycle.intake(Task(id="demo_app", body="# d\n\nbody",
                                   source="directory:demo_app.md",
                                   meta={"demo": True}))
        self.assertIs(self._task_json()["demo"], True)

    def test_intake_writes_demo_false_for_a_plain_task(self):
        self.lifecycle.intake(Task(id="demo_app", body="# d\n\nbody",
                                   source="directory:demo_app.md"))
        self.assertIs(self._task_json()["demo"], False)

    def test_non_bool_meta_coerces_to_bool(self):
        """`task.json` only ever holds a JSON boolean, whatever meta held."""
        self.lifecycle.intake(Task(id="demo_app", body="# d\n\nbody",
                                   meta={"demo": "yes"}))
        self.assertIs(self._task_json()["demo"], True)
        self.assertIs(self.lifecycle.load_state("demo_app").demo, True)

    def test_resume_reloads_the_flag(self):
        self.lifecycle.intake(Task(id="demo_app", body="# d\n\nbody",
                                   meta={"demo": True}))
        # Simulate work between sessions, then a fresh read of the checkpoint.
        self.lifecycle.checkpoint("demo_app", CheckpointStage.SPEC)
        self.lifecycle.set_stage("demo_app", CheckpointStage.SLICING)
        self.assertIs(self.lifecycle.load_state("demo_app").demo, True)

    def test_terminal_status_stamp_keeps_the_flag(self):
        self.lifecycle.intake(Task(id="demo_app", body="# d\n\nbody",
                                   meta={"demo": True}))
        self.lifecycle.fail("demo_app", "deploy failed")
        state = self.lifecycle.load_state("demo_app", where="failed")
        self.assertIs(state.demo, True)
        self.assertIs(self._task_json(where="failed")["demo"], True)
        self.assertFalse(self.lifecycle.task_json_path("demo_app").exists())

    def test_legacy_task_json_without_the_field_loads_false(self):
        self.lifecycle.intake(Task(id="demo_app", body="# d\n\nbody"))
        path = self.lifecycle.task_json_path("demo_app")
        raw = json.loads(path.read_text())
        del raw["demo"]
        path.write_text(json.dumps(raw))
        self.assertIs(self.lifecycle.load_state("demo_app").demo, False)

    def test_legacy_task_json_with_a_falsy_field_loads_false(self):
        """A non-bool value on disk is coerced, never a crash."""
        self.lifecycle.intake(Task(id="demo_app", body="# d\n\nbody"))
        path = self.lifecycle.task_json_path("demo_app")
        raw = json.loads(path.read_text())
        raw["demo"] = 0
        path.write_text(json.dumps(raw))
        self.assertIs(self.lifecycle.load_state("demo_app").demo, False)


class ClaimToIntakeTest(_QueueMixin, unittest.TestCase):
    """The whole bridge: sync sidecar -> claim -> intake -> task.json."""

    def setUp(self):
        self._make_queue()

    def test_claimed_demo_task_intakes_flagged(self):
        self._pending_task()
        provider = self._provider()
        task = provider.fetch_pending(claim=True, owner="run-a")[0]
        lifecycle = self._lifecycle()
        lifecycle.intake(task)
        raw = json.loads(
            lifecycle.task_json_path(task.id).read_text())
        self.assertIs(raw["demo"], True)
        self.assertIs(lifecycle.load_state(task.id).demo, True)

    def test_claimed_plain_task_intakes_unflagged(self):
        self._pending_task(demo=False)
        provider = self._provider()
        task = provider.fetch_pending(claim=True, owner="run-a")[0]
        lifecycle = self._lifecycle()
        lifecycle.intake(task)
        self.assertIs(lifecycle.load_state(task.id).demo, False)
