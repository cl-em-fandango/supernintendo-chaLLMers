"""Slice 1 — the `stage_sync` callable + the ≤ 2000-char body cap.

Covers the journey spec's stage-comment plumbing before any pipeline
site uses it: `StageCommentSync` posts through the shared
`HandoffCommentPoster` (same header format, same sidecar `comment_ids`
dedup, same failed-post retry queue) and never touches a sync engine —
a stage completion must not run an inbound pass (FR-5, NFR-4). Also
covers the FR-4 hard limit: a 3000-char prose yields a body of at most
2000 chars with the header intact, while dedup identity still rides on
the full prose. All in-process: temp queue dirs, a fake API object and
a recording engine stub (NFR-5).

Done-when checks covered here:
  * one comment with the `**[stage]** task …[, slice …][, iteration …]`
    header, no sync-engine method invoked, no pass-summary log line;
  * 3000-char prose ⇒ body ≤ 2000 chars, header intact;
  * a second identical call posts nothing (sidecar dedup);
  * an api exception is logged, the event lands in `_pending_events`,
    and `retry_pending(task_id)` re-posts it (AC5);
  * no engine ⇒ `build_stage_sync` returns None (FR-0.1).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import Comment  # noqa: E402
from harness.composition import build_stage_sync  # noqa: E402
from harness.core import task_record  # noqa: E402
from harness.core.sync_comments import (  # noqa: E402
    MAX_COMMENT_CHARS,
    TRUNCATION_MARKER,
    HandoffCommentPoster,
    HandoffEvent,
    comment_body,
)
from harness.core.sync_handoff_hook import StageCommentSync  # noqa: E402
from tests.legacy_sidecars import SyncLinkage  # noqa: E402

REPO = "acme/widgets"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


class FakeApi:
    """The comment surface only; posts are recorded and failures injected."""

    def __init__(self, raising=False):
        self.posts = []
        self.raising = raising

    def create_comment(self, number, body):
        if self.raising:
            raise RuntimeError("HTTP 500 from github")
        self.posts.append((number, body))
        return Comment(id=100 + len(self.posts), body=body,
                       html_url=f"https://github.com/{REPO}/issues/{number}")

    def list_comments(self, number):
        return []


class RecordingEngine:
    """A sync-engine stand-in that records every method a caller attempts.

    `StageCommentSync` must never reach any of it: the wrapper holds only
    the poster, so a recorded call here would mean a stage comment could
    trigger an inbound pass (NFR-4 violation)."""

    def __init__(self, poster, log):
        self.comment_poster = poster
        self.log = log
        self.calls = []

    def is_in_flight(self, task_id):
        self.calls.append(("is_in_flight", task_id))
        return True

    def on_stage_change(self, task_id=None):
        self.calls.append(("on_stage_change", task_id))
        raise AssertionError("stage_sync must never run a pass")


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        for sub in LOCATIONS:
            (self.queue / sub).mkdir(parents=True)
        self.messages = []

    def linked_task(self, name="mover", issue=7):
        task_dir = self.queue / "active" / name
        (task_dir / "artifacts" / "progress").mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"id": name, "status": "active"}))
        task_record.write_linkage(self.queue, name,
                                  SyncLinkage(issue=issue, repo=REPO))
        return task_dir

    def poster(self, api, **kw):
        return HandoffCommentPoster(api, self.queue, REPO,
                                    log=self.messages.append, **kw)


class CommentBodyLimitTest(unittest.TestCase):
    def test_short_prose_stays_verbatim(self):
        event = HandoffEvent(task_id="t", stage="spec", prose="exact  text")
        self.assertEqual("**[spec]** task t, stage spec\n\nexact  text",
                         comment_body(event))

    def test_long_prose_is_truncated_and_header_survives(self):
        event = HandoffEvent(task_id="mover", stage="slices",
                             prose="x" * 3000, slice_id="2", iteration=3)
        body = comment_body(event)
        self.assertLessEqual(len(body), MAX_COMMENT_CHARS)
        self.assertTrue(body.startswith(
            "**[slices]** task mover, stage slices, slice 2, iteration 3\n\n"))
        self.assertTrue(body.endswith(TRUNCATION_MARKER))

    def test_truncation_keeps_the_prose_prefix(self):
        prose = "".join(f"line {i} of the long summary\n" for i in range(200))
        body = comment_body(HandoffEvent(task_id="t", stage="spec",
                                         prose=prose))
        self.assertLessEqual(len(body), MAX_COMMENT_CHARS)
        self.assertIn("line 0 of the long summary", body)

    def test_header_is_never_truncated_even_without_prose_budget(self):
        event = HandoffEvent(task_id="t", stage="s" * (MAX_COMMENT_CHARS + 50),
                             prose="y" * 3000)
        body = comment_body(event)
        self.assertTrue(body.startswith(f"**[{event.stage}]**"))
        self.assertEqual(body, f"**[{event.stage}]** task t, "
                               f"stage {event.stage}\n\n")


class StageCommentPostTest(SyncTestCase):
    def stage_sync(self, api):
        poster = self.poster(api)
        engine = RecordingEngine(poster, self.messages.append)
        return build_stage_sync(engine), poster, engine

    def test_one_comment_with_the_fr_4_header(self):
        self.linked_task()
        api = FakeApi()
        stage_sync, _, _ = self.stage_sync(api)
        stage_sync("mover", "spec", "spec written to spec.md", "2.1", 3)
        self.assertEqual(1, len(api.posts))
        self.assertEqual(7, api.posts[0][0])
        self.assertEqual(
            "**[spec]** task mover, stage spec, slice 2.1, iteration 3\n\n"
            "spec written to spec.md", api.posts[0][1])

    def test_header_omits_slice_and_iteration_when_absent(self):
        self.linked_task()
        api = FakeApi()
        stage_sync, _, _ = self.stage_sync(api)
        stage_sync("mover", "feasibility", "feasibility: feasible")
        self.assertEqual("**[feasibility]** task mover, stage feasibility\n\n"
                         "feasibility: feasible", api.posts[0][1])

    def test_no_sync_engine_method_and_no_pass_summary_line(self):
        self.linked_task()
        api = FakeApi()
        stage_sync, _, engine = self.stage_sync(api)
        stage_sync("mover", "slicing", "3 slices planned", "1", 1)
        self.assertEqual([], engine.calls)
        self.assertEqual(1, len(api.posts))
        self.assertFalse(any("pass" in m for m in self.messages))

    def test_second_identical_call_posts_nothing(self):
        self.linked_task()
        api = FakeApi()
        stage_sync, _, _ = self.stage_sync(api)
        stage_sync("mover", "slices", "slice 1 merged", "1")
        stage_sync("mover", "slices", "slice 1 merged", "1")
        self.assertEqual(1, len(api.posts))
        linkage = task_record.read_linkage(self.queue, "mover")
        self.assertEqual(1, len(linkage.comment_ids))

    def test_slice_id_is_display_data_not_identity(self):
        """FR-5: the event id rides on the prose, so identical prose for
        two slices dedupes to one post — the composing sites keep events
        distinct by carrying the slice id inside the prose."""
        self.linked_task()
        api = FakeApi()
        stage_sync, _, _ = self.stage_sync(api)
        stage_sync("mover", "slices", "review passed", "1")
        stage_sync("mover", "slices", "review passed", "2")
        self.assertEqual(1, len(api.posts))
        stage_sync("mover", "slices", "slice 2: review passed", "2")
        self.assertEqual(2, len(api.posts))

    def test_truncated_body_still_dedupes_on_full_prose(self):
        """Identity rides on the full prose, not the truncated body: two
        long proses differing only past the cut are two events."""
        self.linked_task()
        api = FakeApi()
        stage_sync, _, _ = self.stage_sync(api)
        stage_sync("mover", "spec", "x" * 3000)
        stage_sync("mover", "spec", "x" * 3000 + " tail")
        self.assertEqual(2, len(api.posts))
        self.assertTrue(all(len(body) <= MAX_COMMENT_CHARS
                            for _, body in api.posts))

    def test_posted_body_respects_the_2000_char_cap(self):
        self.linked_task()
        api = FakeApi()
        stage_sync, _, _ = self.stage_sync(api)
        stage_sync("mover", "slices", "y" * 3000, "2", 2)
        body = api.posts[0][1]
        self.assertLessEqual(len(body), MAX_COMMENT_CHARS)
        self.assertTrue(body.startswith(
            "**[slices]** task mover, stage slices, slice 2, iteration 2\n\n"))


class StageCommentFailureTest(SyncTestCase):
    def test_api_exception_is_logged_queued_and_retried(self):
        """AC5: the call swallows the failure, the event waits in the
        poster's pending queue, and retry_pending re-posts it."""
        self.linked_task()
        api = FakeApi(raising=True)
        poster = self.poster(api)
        stage_sync = StageCommentSync(poster, log=self.messages.append)
        stage_sync("mover", "merge", "merged onto trunk", None, None)
        self.assertEqual([], api.posts)
        self.assertTrue(any("handoff comment failed" in m
                            for m in self.messages))
        self.assertEqual(1, len(poster._pending_events))
        api.raising = False
        self.assertEqual(1, poster.retry_pending("mover"))
        self.assertEqual(1, len(api.posts))
        self.assertIn("**[merge]** task mover, stage merge\n\n"
                      "merged onto trunk", api.posts[0][1])
        self.assertEqual(0, len(poster._pending_events))

    def test_pending_event_for_another_task_stays_queued(self):
        self.linked_task()
        api = FakeApi(raising=True)
        poster = self.poster(api)
        stage_sync = StageCommentSync(poster, log=self.messages.append)
        stage_sync("mover", "spec", "spec prose")
        self.assertEqual(0, poster.retry_pending("other_task"))
        self.assertEqual(1, len(poster._pending_events))

    def test_broken_poster_is_logged_not_raised(self):
        """NFR-1 at the wrapper itself: a poster object that cannot even
        be called must not escape into the pipeline."""
        def broken_poster(*args, **kwargs):
            raise RuntimeError("poster exploded")
        stage_sync = StageCommentSync(broken_poster,
                                      log=self.messages.append)
        stage_sync("mover", "spec", "prose")  # must not raise
        self.assertTrue(any("stage comment failed" in m
                            for m in self.messages))


class StageSyncCompositionTest(unittest.TestCase):
    def test_no_engine_means_no_stage_sync(self):
        """FR-0.1: GitHub unconfigured ⇒ the factory yields None and the
        hook sites stay no-ops."""
        self.assertIsNone(build_stage_sync(None))

    def test_factory_shares_the_engines_poster(self):
        poster = object()
        engine = RecordingEngine(poster, print)
        stage_sync = build_stage_sync(engine)
        self.assertIsInstance(stage_sync, StageCommentSync)
        self.assertIs(poster, stage_sync.poster)
        self.assertFalse(hasattr(stage_sync, "engine"))


if __name__ == "__main__":
    unittest.main()
