"""Slice 6 — handoff comments with content-stable dedup (spec FR-2.5, AC-8).

Covers: the exact `**[stage]** …` + verbatim-prose format; an event id
built from issue number + task id + stage + prose hash only (stable across
passes, changed by prose, untouched by timestamps or attempt counters);
dedup through the sidecar `comment_ids` map; the three instrumented write
sites (`continuation.write_note`, the `task_lifecycle` `Handoff` section,
and the terminal executive-summary writers); and NFR-1 (a raising API
leaves the pipeline path untouched). All in-process: temp queue dirs and a
fake API object (NFR-5).

Done-when checks covered here:
  * AC-8: one comment per handover event, a retried pass posts nothing new;
  * format matches FR-2.5 exactly;
  * id stable across repeated passes with identical prose, changes when
    prose changes;
  * a raising fake API leaves the pipeline path unaffected.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import Comment  # noqa: E402
from harness.core.config import load  # noqa: E402
from harness.core import task_record  # noqa: E402
from harness.core.sync_comments import (  # noqa: E402
    HandoffCommentPoster,
    HandoffEvent,
    comment_body,
    event_id,
)
from tests.legacy_sidecars import (  # noqa: E402
    SyncLinkage,
    file_sidecar_path,
    write_legacy_linkage,
)
from harness.workflow.continuation import ContinuationNote, write_note  # noqa: E402
from harness.workflow.task_lifecycle import Handoff, TaskLifecycle  # noqa: E402

REPO = "acme/widgets"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


class FakeApi:
    """The comment surface only; posts are recorded and dedup is observable."""

    def __init__(self, raising=False):
        self.comments: dict[int, list[Comment]] = {}
        self.posts = []
        self.raising = raising
        self._next_id = 100

    def create_comment(self, number, body):
        if self.raising:
            raise RuntimeError("HTTP 500 from github")
        self.posts.append((number, body))
        self._next_id += 1
        comment = Comment(id=self._next_id, body=body,
                          html_url=f"https://github.com/{REPO}/issues/{number}#{self._next_id}")
        self.comments.setdefault(number, []).append(comment)
        return comment

    def list_comments(self, number):
        return self.comments.get(number, [])


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        for sub in LOCATIONS:
            (self.queue / sub).mkdir(parents=True)
        cfg_path = self.work_dir / "config.json"
        cfg_path.write_text(json.dumps({
            "harnessExecutionAndQueueDir": str(self.work_dir), "githubPat": "ghp_token",
            "githubRepo": REPO}))
        self.cfg = load(cfg_path)
        self.messages = []

    def active_dir_task(self, name, issue=7):
        task_dir = self.queue / "active" / name
        (task_dir / "artifacts" / "progress").mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"id": name, "status": "active"}))
        (task_dir / "original.md").write_text(f"# {name} body")
        task_record.write_linkage(self.queue, name,
                              SyncLinkage(issue=issue, repo=REPO))
        return task_dir

    def poster(self, api, **kw):
        return HandoffCommentPoster(api, self.queue, REPO,
                                    log=self.messages.append, **kw)

    def event(self, prose="we stopped at the cap", task="mover", stage="implement"):
        return HandoffEvent(task_id=task, stage=stage, prose=prose)


def make_event(prose, task="mover", stage="implement"):
    return HandoffEvent(task_id=task, stage=stage, prose=prose)


class EventIdTest(unittest.TestCase):
    def test_id_is_stable_across_passes_and_objects(self):
        first = event_id(7, make_event("same prose"))
        second = event_id(7, make_event("same prose"))
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_id_changes_with_prose_issue_task_or_stage(self):
        base = event_id(7, make_event("p"))
        self.assertNotEqual(base, event_id(7, make_event("p changed")))
        self.assertNotEqual(base, event_id(8, make_event("p")))
        self.assertNotEqual(base, event_id(7, make_event("p", task="other")))
        self.assertNotEqual(base, event_id(7, make_event("p", stage="review")))

    def test_id_carries_no_timestamp_or_attempt_material(self):
        """Recomputing after wall-clock time passes yields the same id, and
        an `attempt`-like display change does not alter it either."""
        import time
        first = event_id(3, make_event("p"))
        time.sleep(0.01)
        self.assertEqual(first, event_id(3, make_event("p")))
        later = HandoffEvent(task_id="t", stage="implement", prose="p",
                             slice_id="4", iteration=9)
        self.assertEqual(event_id(3, HandoffEvent(task_id="t",
                                                  stage="implement",
                                                  prose="p")),
                         event_id(3, later))


class CommentFormatTest(unittest.TestCase):
    def test_format_matches_fr_2_5(self):
        event = HandoffEvent(task_id="fix_the_parser", stage="implement",
                             prose="line one\nline two", slice_id="2.1",
                             iteration=3)
        self.assertEqual(
            "**[implement]** task fix_the_parser, stage implement, "
            "slice 2.1, iteration 3\n\nline one\nline two",
            comment_body(event))

    def test_context_line_omits_slice_and_iteration_when_absent(self):
        body = comment_body(HandoffEvent(task_id="t", stage="spec",
                                         prose="prose"))
        self.assertEqual("**[spec]** task t, stage spec\n\nprose", body)


class PosterTest(SyncTestCase):
    def test_ac8_one_comment_then_retried_pass_posts_nothing(self):
        task_dir = self.active_dir_task("mover")
        api = FakeApi()
        poster = self.poster(api)
        poster.post(self.event())
        self.assertEqual(1, len(api.posts))
        self.assertEqual(7, api.posts[0][0])
        self.assertIn("**[implement]**", api.posts[0][1])
        # The dedup map is written through to the sidecar.
        linkage = task_record.read_linkage(self.queue, "mover")
        self.assertEqual(1, len(linkage.comment_ids))
        # A retried pass with identical prose posts nothing new.
        poster.post(self.event())
        self.poster(FakeApi()).post(self.event())
        self.assertEqual(1, len(api.posts))

    def test_changed_prose_is_a_new_comment(self):
        self.active_dir_task("mover")
        api = FakeApi()
        poster = self.poster(api)
        poster.post(self.event("first prose"))
        poster.post(self.event("second prose"))
        self.assertEqual(2, len(api.posts))

    def test_prose_is_posted_verbatim(self):
        self.active_dir_task("mover")
        api = FakeApi()
        prose = "exact text\n  with  spacing\n**bold** and `code`"
        self.poster(api).post(self.event(prose))
        self.assertTrue(api.posts[0][1].endswith(prose))

    def test_task_without_linkage_is_a_silent_skip(self):
        task_dir = self.queue / "active" / "unlinked"
        task_dir.mkdir()
        api = FakeApi()
        self.assertIsNone(self.poster(api).post(self.event(task="unlinked")))
        self.assertEqual([], api.posts)

    def test_task_absent_from_disk_is_a_silent_skip(self):
        api = FakeApi()
        self.assertIsNone(self.poster(api).post(self.event(task="gone")))
        self.assertEqual([], api.posts)

    def test_linkage_to_another_repo_is_skipped(self):
        task_dir = self.queue / "active" / "foreign"
        task_dir.mkdir()
        task_record.write_linkage(self.queue, "foreign",
                              SyncLinkage(issue=9, repo="other/repo"))
        api = FakeApi()
        self.assertIsNone(self.poster(api).post(self.event(task="foreign")))
        self.assertEqual([], api.posts)

    def test_file_task_uses_the_record(self):
        task_file = self.queue / "pending" / "queued.md"
        task_file.write_text("# queued")
        task_record.write_linkage(self.queue, "queued",
                              SyncLinkage(issue=4, repo=REPO))
        api = FakeApi()
        self.poster(api).post(self.event(task="queued"))
        self.assertEqual(4, api.posts[0][0])
        self.assertEqual(
            1, len(task_record.read_linkage(self.queue, "queued").comment_ids))

    def test_a_legacy_file_sidecar_is_adopted_and_retired(self):
        """FR-E2/FR-B4: a pre-record `X.md.gh.json` still dedupes a handoff,
        and the post lands in the record with the legacy file gone (FR-E3).
        """
        task_file = self.queue / "pending" / "queued.md"
        task_file.write_text("# queued")
        legacy = file_sidecar_path(task_file)
        write_legacy_linkage(legacy, SyncLinkage(issue=4, repo=REPO))
        api = FakeApi()
        poster = self.poster(api)
        poster.post(self.event(task="queued"))
        self.assertEqual(4, api.posts[0][0])
        self.assertFalse(legacy.exists())
        self.assertEqual(
            1, len(task_record.read_linkage(self.queue, "queued").comment_ids))
        poster.post(self.event(task="queued"))
        self.assertEqual(1, len(api.posts))

    def test_a_task_in_two_locations_has_one_linkage(self):
        """An active dir and the review summary file of the same name are one
        task with one record — the precedence the file-derived sidecars
        forced is gone (FR-A2).
        """
        self.active_dir_task("finished", issue=11)
        (self.queue / "review" / "finished.md").write_text("# summary")
        api = FakeApi()
        self.poster(api).post(self.event(task="finished"))
        self.assertEqual([11], [number for number, _ in api.posts])
        self.assertEqual(
            1,
            len(task_record.read_linkage(self.queue, "finished").comment_ids))

    def test_verify_mode_reads_comments_back(self):
        self.active_dir_task("mover")
        api = FakeApi()
        self.poster(api, verify=True).post(self.event())
        self.assertEqual(1, len(api.list_comments(7)))

    def test_call_swallows_api_failure_and_keeps_dedup_clean(self):
        """NFR-1: a raising API must not escape the hook site, and must not
        record a dedup id for a comment that was never posted."""
        task_dir = self.active_dir_task("mover")
        poster = self.poster(FakeApi(raising=True))
        poster("mover", "implement", "prose", None, None)  # must not raise
        self.assertEqual({}, task_record.read_linkage(self.queue, "mover").comment_ids)
        self.assertTrue(any("handoff comment failed" in m
                            for m in self.messages))


class ContinuationSiteTest(SyncTestCase):
    def test_write_note_posts_one_comment_and_retries_are_deduped(self):
        task_dir = self.active_dir_task("mover")
        api = FakeApi()
        poster = self.poster(api)
        note = ContinuationNote(stage="implement", attempt=1, peak_tokens=90000,
                                context_limit=80000, slice_id="2",
                                iteration=1, task_id="mover")
        written = write_note(task_dir / "artifacts" / "progress", note,
                             "partial output text", handoff_sync=poster)
        self.assertEqual(1, len(api.posts))
        self.assertIn("**[implement]**", api.posts[0][1])
        self.assertIn("partial output text", api.posts[0][1])
        self.assertIn("task mover, stage implement, slice 2, iteration 1",
                      api.posts[0][1])
        # The note itself is still written, and a replay of the same event
        # (same prose) posts nothing new.
        self.assertTrue(written.note_path.is_file())
        write_note(task_dir / "artifacts" / "progress", note,
                   "partial output text", handoff_sync=poster)
        self.assertEqual(1, len(api.posts))
        self.assertEqual(1, len(task_record.read_linkage(self.queue, "mover").comment_ids))

    def test_write_note_without_poster_is_unchanged(self):
        task_dir = self.active_dir_task("mover")
        note = ContinuationNote(stage="implement", attempt=1, peak_tokens=1,
                                task_id="mover")
        written = write_note(task_dir / "artifacts" / "progress", note, "text")
        self.assertTrue(written.note_path.is_file())

    def test_write_note_with_raising_poster_still_writes_the_note(self):
        task_dir = self.active_dir_task("mover")
        note = ContinuationNote(stage="implement", attempt=1, peak_tokens=1,
                                task_id="mover")
        written = write_note(task_dir / "artifacts" / "progress", note, "text",
                             handoff_sync=self.poster(FakeApi(raising=True)))
        self.assertTrue(written.note_path.is_file())

    def test_note_without_task_id_posts_nothing(self):
        api = FakeApi()
        poster = self.poster(api)
        note = ContinuationNote(stage="implement", attempt=1, peak_tokens=1)
        out = Path(self._tmp.name) / "bare"
        write_note(out, note, "text", handoff_sync=poster)
        self.assertEqual([], api.posts)


class TerminalSummarySiteTest(SyncTestCase):
    def lifecycle(self):
        return TaskLifecycle(self.cfg, log=self.messages.append,
                             handoff_sync=self.poster(FakeApi()))

    def test_complete_posts_the_executive_summary_once(self):
        task_dir = self.active_dir_task("mover")
        api = FakeApi()
        lifecycle = TaskLifecycle(self.cfg, log=self.messages.append,
                                  handoff_sync=self.poster(api))
        lifecycle.complete("mover", "Feature complete and merged")
        self.assertEqual(1, len(api.posts))
        self.assertIn("**[DONE]** task mover, stage DONE\n\n", api.posts[0][1])
        self.assertIn("Feature complete and merged", api.posts[0][1])
        # A retried pass over the same summary posts nothing new.
        lifecycle.complete("mover", "Feature complete and merged")
        self.assertEqual(1, len(api.posts))
        self.assertEqual(
            1, len(task_record.read_linkage(self.queue, "mover").comment_ids))

    def test_fail_posts_the_kickout_summary(self):
        self.active_dir_task("mover")
        api = FakeApi()
        lifecycle = TaskLifecycle(self.cfg, log=self.messages.append,
                                  handoff_sync=self.poster(api))
        lifecycle.fail("mover", "review rejected twice")
        self.assertIn("**[KICKED OUT]**", api.posts[0][1])
        self.assertIn("review rejected twice", api.posts[0][1])

    def test_park_with_handoff_posts_the_handoff_prose(self):
        self.active_dir_task("mover")
        api = FakeApi()
        lifecycle = TaskLifecycle(self.cfg, log=self.messages.append,
                                  handoff_sync=self.poster(api))
        lifecycle.park("mover", "over context cap",
                       handoff=Handoff(stage="implement", slice_id="3",
                                       iteration=2, peak_tokens=99000,
                                       context_limit=100000))
        body = api.posts[0][1]
        self.assertIn("**[implement]**", body)
        self.assertIn("slice 3", body)
        self.assertIn("## Handoff", body)
        self.assertIn("## Next agent should", body)

    def test_park_without_handoff_posts_the_reason(self):
        self.active_dir_task("mover")
        api = FakeApi()
        lifecycle = TaskLifecycle(self.cfg, log=self.messages.append,
                                  handoff_sync=self.poster(api))
        lifecycle.park("mover", "parked via GitHub issue #12")
        self.assertIn("**[PARKED]**", api.posts[0][1])
        self.assertIn("parked via GitHub issue #12", api.posts[0][1])

    def test_lifecycle_without_poster_moves_normally(self):
        """NFR-2 shape: no poster wired -> the move is byte-identical work
        with no sync involvement."""
        self.active_dir_task("mover")
        TaskLifecycle(self.cfg, log=self.messages.append).complete(
            "mover", "done")
        self.assertTrue((self.queue / "done" / "mover").is_dir())

    def test_raising_poster_does_not_change_the_move(self):
        """NFR-1 at the lifecycle site: the task still lands in done/."""
        self.active_dir_task("mover")
        lifecycle = TaskLifecycle(self.cfg, log=self.messages.append,
                                  handoff_sync=self.poster(FakeApi(raising=True)))
        lifecycle.complete("mover", "summary")
        self.assertTrue((self.queue / "done" / "mover").is_dir())
        self.assertTrue(any("handoff comment failed" in m
                            for m in self.messages))


if __name__ == "__main__":
    unittest.main()
