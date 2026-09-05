"""Slice 2 — stage-completion comments for spec / feasibility / slicing.

The standard pipeline posts exactly one summary comment per STAGE_SEQUENCE
checkpoint through the opaque `stage_sync` callable (journey spec FR-1),
with a harness-composed factual summary (FR-4): verdict plus artifact
counts, never transcript text. Stages skipped on resume post nothing
(FR-2), a stage that fails or raises posts nothing (its failure is covered
by the existing handoff/terminal comment), and with `stage_sync=None`
(GitHub unconfigured) the pipeline is entirely unaffected (FR-0.1).

All in-process: temp queue dirs, a plain git repo as workdir, stub stage
functions, a fake API recording `create_comment` (no network, no `pi`).

Done-when checks covered here:
  * a stubbed run completing spec → feasibility → slicing produces 3
    comments in order, correct wire stage in each header, 2–6-line
    factual summaries (AC1 partial);
  * a resumed task with `spec` checkpointed posts no spec comment (AC3);
  * a stage that fails or raises posts no stage comment (AC4 partial);
  * `stage_sync=None`: zero api calls, pipeline unaffected (AC6).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import Comment  # noqa: E402
from harness.core import task_record  # noqa: E402
from harness.core.config import Config  # noqa: E402
from harness.core.enums import CheckpointStage  # noqa: E402
from harness.core.providers import Task  # noqa: E402
from harness.core.sync_comments import HandoffCommentPoster  # noqa: E402
from harness.core.sync_handoff_hook import StageCommentSync  # noqa: E402
from harness.workflow.pipeline import (  # noqa: E402
    Pipeline,
    stage_completion_summary,
)
from harness.workflow.task_lifecycle import TaskLifecycle  # noqa: E402
from tests.legacy_sidecars import SyncLinkage  # noqa: E402

GITHUB_REPO = "acme/widgets"
QUEUE_LOCATIONS = ("pending", "claimed", "active", "review",
                   "parked", "failed", "done")
SLICES_MD = ("# Slices\n\n"
             "### Slice 1\n\ndo A\n\n"
             "### Slice 2\n\ndo B\n\n"
             "### Slice 3\n\ndo C\n")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _make_repo(root: Path) -> Path:
    """A minimal git repo on pi/trunk — enough for `ensure_branch`."""
    root.mkdir(parents=True)
    (root / "README.md").write_text("target repo\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "init")
    return root


class FakeApi:
    """The comment surface only; posts are recorded."""

    def __init__(self):
        self.posts = []

    def create_comment(self, number, body):
        self.posts.append((number, body))
        return Comment(id=100 + len(self.posts), body=body,
                       html_url=f"https://github.com/{GITHUB_REPO}/issues/{number}")

    def list_comments(self, number):
        return []


class NullRunner:
    """No sessions run: every stage function is stubbed on the pipeline."""


class StageCompletionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue_dir = self.work_dir / "queue"
        for sub in QUEUE_LOCATIONS:
            (self.queue_dir / sub).mkdir(parents=True)
        self.repo = _make_repo(self.work_dir / "repo")
        self.cfg = Config(
            work_dir=self.work_dir,
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
            models={"technicalWriter": "m", "implementer": "m",
                    "assessor": "m"},
            model_context_map={},
            repo_dir=self.repo,
        )
        self.lines: list[str] = []
        self.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        self.api = FakeApi()
        poster = HandoffCommentPoster(self.api, self.queue_dir, GITHUB_REPO,
                                      log=self.lines.append)
        self.stage_sync = StageCommentSync(poster, log=self.lines.append)
        self.pipeline = Pipeline(self.cfg, NullRunner(),
                                 log=self.lines.append,
                                 stage_sync=self.stage_sync)
        self.stage_calls: list[str] = []

    # ------------------------------------------------------------------
    # stub stages: record the call, write the stage's durable artifact,
    # succeed — unless the test marks the stage as failing/raising.
    # ------------------------------------------------------------------
    def _stub_stages(self, fail_at=None, raise_at=None, pipeline=None):
        target = pipeline if pipeline is not None else self.pipeline
        artifacts = {"spec": "spec.md", "slicing": "slices.md"}

        def make(name):
            def stage(ctx):
                self.stage_calls.append(name)
                if name == raise_at:
                    raise RuntimeError(f"{name} exploded")
                artifact = artifacts.get(name)
                if artifact:
                    path = ctx.task_dir / "artifacts" / artifact
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(SLICES_MD if artifact == "slices.md"
                                    else "# spec\n")
                return name != fail_at
            return stage

        for checkpoint in ("spec", "feasibility", "slicing", "slices"):
            setattr(target, f"stage_{checkpoint}", make(checkpoint))
        target.stage_holistic = lambda ctx: "done"

    def _intake_linked_task(self) -> Task:
        """Intake through the lifecycle (so the sidecar can be written
        before the run) and link the task to issue 7. `process` then
        takes the resume path with zero checkpoints — the same waterfall
        a fresh intake runs, with a linkage the poster can post to."""
        task = Task(id="t1", body="# t1\n\ndeliver the thing\n",
                    source="directory:t1.md")
        self.lifecycle.intake(task)
        task_record.write_linkage(self.queue_dir, "t1",
                                  SyncLinkage(issue=7, repo=GITHUB_REPO))
        return task

    def _bodies(self):
        return [body for _, body in self.api.posts]

    def _headers(self):
        return [body.splitlines()[0] for body in self._bodies()]

    # ------------------------------------------------------------------
    # Done-when: spec → feasibility → slicing yields 3 ordered comments
    # with the wire stage in the header and a factual 2–6 line summary.
    # ------------------------------------------------------------------
    def test_three_completed_stages_post_three_ordered_comments(self):
        self._stub_stages(fail_at="slices")  # stop the run after slicing
        task = self._intake_linked_task()
        self.pipeline.process(task)
        self.assertEqual([
            "**[spec]** task t1, stage spec",
            "**[feasibility]** task t1, stage feasibility",
            "**[slicing]** task t1, stage slicing",
        ], self._headers())

    def test_comment_order_matches_checkpoint_order(self):
        self._stub_stages()
        task = self._intake_linked_task()
        self.pipeline.process(task)
        self.assertEqual([
            "**[spec]** task t1, stage spec",
            "**[feasibility]** task t1, stage feasibility",
            "**[slicing]** task t1, stage slicing",
            "**[slices]** task t1, stage slices",
        ], self._headers())
        self.assertEqual([7, 7, 7, 7], [n for n, _ in self.api.posts])

    def test_summaries_are_factual_and_transcript_free(self):
        self._stub_stages()
        task = self._intake_linked_task()
        self.pipeline.process(task)
        summaries = [body.split("\n\n", 1)[1] for body in self._bodies()]
        self.assertEqual("outcome: pass\nspec written to artifacts/spec.md",
                         summaries[0])
        self.assertEqual("outcome: pass\nfeasibility: feasible",
                         summaries[1])
        self.assertEqual("outcome: pass\n3 slices planned", summaries[2])
        self.assertEqual("outcome: pass\n3 slices completed", summaries[3])
        for summary in summaries:
            self.assertGreaterEqual(len(summary.splitlines()), 2)
            self.assertLessEqual(len(summary.splitlines()), 6)

    # ------------------------------------------------------------------
    # AC3: a resumed task posts nothing for checkpointed stages.
    # ------------------------------------------------------------------
    def test_resume_posts_no_comment_for_checkpointed_stage(self):
        self._stub_stages()
        task = self._intake_linked_task()
        self.lifecycle.checkpoint("t1", CheckpointStage.SPEC)
        self.pipeline.process(task)
        self.assertNotIn("spec", self.stage_calls)
        self.assertEqual([
            "**[feasibility]** task t1, stage feasibility",
            "**[slicing]** task t1, stage slicing",
            "**[slices]** task t1, stage slices",
        ], self._headers())

    # ------------------------------------------------------------------
    # AC4 partial: a stage that fails or raises posts no stage comment.
    # ------------------------------------------------------------------
    def test_failing_stage_posts_no_comment(self):
        self._stub_stages(fail_at="feasibility")
        task = self._intake_linked_task()
        outcome = self.pipeline.process(task)
        self.assertEqual("parked", outcome)
        self.assertEqual(["**[spec]** task t1, stage spec"],
                         self._headers())

    def test_raising_stage_posts_no_comment(self):
        self._stub_stages(raise_at="feasibility")
        task = self._intake_linked_task()
        with self.assertRaises(RuntimeError):
            self.pipeline.process(task)
        self.assertEqual(["**[spec]** task t1, stage spec"],
                         self._headers())

    # ------------------------------------------------------------------
    # AC6: no stage_sync (GitHub unconfigured) — zero api calls, the
    # waterfall is untouched.
    # ------------------------------------------------------------------
    def test_no_stage_sync_leaves_pipeline_unaffected(self):
        self._stub_stages()
        task = self._intake_linked_task()
        pipeline = Pipeline(self.cfg, NullRunner(), log=self.lines.append)
        self.assertIsNone(pipeline.stage_sync)
        self._stub_stages(pipeline=pipeline)
        outcome = pipeline.process(task)
        self.assertEqual("done", outcome)
        self.assertEqual([], self.api.posts)

    def test_broken_summary_still_posts_the_bare_outcome(self):
        """NFR-1 at the composition site: a summary that cannot be read
        (a corrupt artifact) costs the detail, not the comment."""
        self._stub_stages()
        task = self._intake_linked_task()
        import harness.workflow.pipeline as pipeline_module
        original = pipeline_module._parse_slices

        def exploding_parser(path):
            raise OSError("unreadable artifacts dir")
        pipeline_module._parse_slices = exploding_parser
        self.addCleanup(setattr, pipeline_module, "_parse_slices", original)
        self.pipeline.process(task)
        self.assertEqual(4, len(self.api.posts))
        slicing_body = self._bodies()[2]
        self.assertTrue(slicing_body.endswith("outcome: pass"))
        self.assertTrue(any("could not compose the slicing stage summary"
                            in line for line in self.lines))


class StageSummaryCompositionTest(unittest.TestCase):
    """The helper is pure: stage + task dir in, factual lines out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.task_dir = Path(self._tmp.name)
        (self.task_dir / "artifacts").mkdir()

    def test_spec_without_artifact_degrades_to_bare_approval(self):
        self.assertEqual("outcome: pass\nspec approved",
                         stage_completion_summary(CheckpointStage.SPEC,
                                                  self.task_dir))

    def test_spec_with_artifact_names_it(self):
        (self.task_dir / "artifacts" / "spec.md").write_text("# spec\n")
        self.assertEqual(
            "outcome: pass\nspec written to artifacts/spec.md",
            stage_completion_summary(CheckpointStage.SPEC, self.task_dir))

    def test_feasibility_states_the_verdict(self):
        self.assertEqual("outcome: pass\nfeasibility: feasible",
                         stage_completion_summary(CheckpointStage.FEASIBILITY,
                                                  self.task_dir))

    def test_slicing_counts_the_plan(self):
        (self.task_dir / "artifacts" / "slices.md").write_text(SLICES_MD)
        self.assertEqual("outcome: pass\n3 slices planned",
                         stage_completion_summary(CheckpointStage.SLICING,
                                                  self.task_dir))
        self.assertEqual("outcome: pass\n3 slices completed",
                         stage_completion_summary(CheckpointStage.SLICES,
                                                  self.task_dir))

    def test_slicing_without_a_plan_degrades_without_raising(self):
        self.assertEqual(
            "outcome: pass\nslice plan written to artifacts/slices.md",
            stage_completion_summary(CheckpointStage.SLICING, self.task_dir))
        self.assertEqual("outcome: pass\nall slices reviewed",
                         stage_completion_summary(CheckpointStage.SLICES,
                                                  self.task_dir))


if __name__ == "__main__":
    unittest.main()
