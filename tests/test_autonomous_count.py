"""T58: the autonomous pending count is read-only.

`AutonomousGenerator._pending_count` used to be `len(provider.fetch_pending())`
— a queue depth bought with a fetch. A fetch is the claim boundary: it is where
`claim=True` lives, where the enqueue guard runs and where an ownership sidecar
is written, so a flip of that default (or a caller that passed `claim=True` by
habit) would empty the queue one file per look. The generator asks on every
loop condition, every attempt header and in its closing line, so the failure is
silent: it stops having "reached target" over an empty `pending/`.

The provider now answers depth directly with `count_pending()`. The directory
provider counts the files a fetch *would* hand over — the enqueue guard still
applies, so a plan parent is neither fetched nor counted — without renaming,
without a sidecar, and without calling `fetch_pending()` at all.

These tests pin, without a subprocess:
- the count equals a fetch of the same queue and skips what the guard refuses;
- the base-interface default (ask the fetch) for an adapter with no cheap count;
- `pending/` and `claimed/` byte-identical, every file in both, before and after
  counting — from the provider and from a whole `AutonomousGenerator.run()`;
- counting never reaches `Path.rename`, so no claim is reachable from it;
- a flipped `claim` default on `fetch_pending` cannot leak into the count;
- `_pending_count` and the generator loop ask the provider to count, never to
  fetch.

Out of scope: generation policy, claims, handlers, and any queue mutation.

Run from the repo root:  python3 -m unittest tests.test_autonomous_count
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core.config import Config  # noqa: E402
from harness.core.enums import Verdict  # noqa: E402
from harness.core.providers import (  # noqa: E402
    DirectoryTaskProvider,
    Task,
    TaskProvider,
)
from harness.core.session import SessionResult  # noqa: E402
from harness.workflow.autonomous import AutonomousGenerator  # noqa: E402

# A plan parent carrying the directive the enqueue guard refuses (line 3, in a
# blockquote), and an ordinary task it allows.
PARENT_BODY = """# T04 — Squash failure-cleanup epic (superseded)

> **DO NOT EXECUTE THIS FILE AS A CARD.** Execute T72 then T73. This file is
> retained as the parent contract and conflict reproduction.
"""

TASK_BODY = """# T99 — Add a status line

## Do
Print the pending count.
"""


def _snapshot(directory: Path) -> dict[str, bytes]:
    """Every file under `directory`, as relative path -> exact bytes.

    Deliberately not a `*.md` glob: a claim leaves a `.claim.json` sidecar (and
    a `.tmp` while one is being written), and a markdown-only view would call a
    freshly written owner "byte-identical".
    """
    return {str(p.relative_to(directory)): p.read_bytes()
            for p in sorted(directory.rglob("*")) if p.is_file()}


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob("*.md"))


def _cfg(work_dir: Path, target: int) -> Config:
    """A config for the generator: only the queue target and a model pool are
    read on the counting path, but the dataclass demands the whole shape."""
    return Config(
        harness_execution_and_queue_dir=work_dir,
        token_budget=100_000,
        max_spec_kickbacks=3,
        max_slice_implement=5,
        max_slice_tech_review=5,
        max_slice_func_review=5,
        max_slice_check_loops=3,
        autonomous_queue_target=target,
        trunk_branch="pi/trunk",
        task_provider="directory",
        directory_provider={},
        models={"technicalWriter": "m", "implementer": "m", "assessor": "m",
                "randomPool": ["model-a", "model-b"]},
        model_context_map={},
    )


class QueueFixture:
    """A temp queue with a pending/ and a claimed/ directory."""

    def __init__(self, test: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.queue = self.root / "queue"
        self.pending = self.queue / "pending"
        self.claimed = self.queue / "claimed"
        self.pending.mkdir(parents=True)
        self.claimed.mkdir(parents=True)
        self.lines: list[str] = []

    def seed(self, pending: dict[str, str], claimed: dict[str, str]) -> None:
        for name, body in pending.items():
            (self.pending / name).write_text(body)
        for name, body in claimed.items():
            (self.claimed / name).write_text(body)

    def snapshot(self) -> dict[str, dict[str, bytes]]:
        """Both queue directories at once — the pair a claim would move between."""
        return {"pending": _snapshot(self.pending),
                "claimed": _snapshot(self.claimed)}


class CountPendingTest(unittest.TestCase):
    """`DirectoryTaskProvider.count_pending()` on a real temp queue."""

    def setUp(self):
        self.q = QueueFixture(self)
        self.provider = DirectoryTaskProvider(self.q.pending, self.q.claimed,
                                              log=self.q.lines.append)

    def test_count_matches_a_fetch_of_the_same_queue(self):
        self.q.seed({"001-a.md": TASK_BODY, "002-b.md": TASK_BODY,
                     "003-c.md": TASK_BODY}, {})
        self.assertEqual(self.provider.count_pending(), 3)
        self.assertEqual(self.provider.count_pending(),
                         len(self.provider.fetch_pending()))

    def test_count_on_an_empty_queue_is_zero(self):
        self.assertEqual(self.provider.count_pending(), 0)

    def test_a_claimed_file_is_not_counted_as_pending(self):
        self.q.seed({"001-a.md": TASK_BODY, "002-b.md": TASK_BODY}, {})
        self.provider.fetch_pending(claim=True, limit=1, owner="run-1")
        self.assertEqual(self.provider.count_pending(), 1)

    def test_a_plan_parent_the_guard_refuses_is_not_counted(self):
        """The count is what a fetch would hand over, so a parent the guard
        refuses is invisible to both — the generator stops on this number and
        must stop on the queue it can actually work."""
        self.q.seed({"001-a.md": TASK_BODY, "T04-parent.md": PARENT_BODY}, {})
        self.assertEqual(self.provider.count_pending(), 1)
        self.assertEqual(self.provider.count_pending(),
                         len(self.provider.fetch_pending()))
        self.assertEqual(_names(self.q.pending), ["001-a.md", "T04-parent.md"],
                         "counting must not remove what it refuses")

    def test_counting_leaves_both_directories_byte_identical(self):
        """The card's proof: pending/ and claimed/ unchanged, byte for byte.

        The claimed file carries a real ownership sidecar, so a stray write
        beside a claim is caught too, and the refused parent stays in pending/
        as the archive it is.
        """
        self.q.seed({"001-a.md": TASK_BODY, "T04-parent.md": PARENT_BODY},
                    {"009-z.md": TASK_BODY})
        (self.q.claimed / "009-z.md.claim.json").write_text(
            '{"version": 1, "owner": "run-1", "claimed_at": 1.0}\n')
        before = self.q.snapshot()
        self.assertEqual(self.provider.count_pending(), 1)
        self.assertEqual(self.q.snapshot(), before)

    def test_counting_never_reaches_for_a_rename(self):
        """A claim is a rename and nothing else. With rename unavailable the
        count is still correct, so the count never needed it."""
        self.q.seed({"001-a.md": TASK_BODY, "002-b.md": TASK_BODY},
                    {"009-z.md": TASK_BODY})

        def no_rename(self_path, target):
            raise AssertionError(f"counting tried to move {self_path.name}")

        with mock.patch.object(Path, "rename", no_rename):
            self.assertEqual(self.provider.count_pending(), 2)
        self.assertEqual(_names(self.q.pending), ["001-a.md", "002-b.md"])
        self.assertEqual(_names(self.q.claimed), ["009-z.md"])

    def test_a_flipped_claim_default_cannot_reach_the_count(self):
        """The regression this card exists for.

        `fetch_pending`'s `claim` default is False today. Simulated flipped, a
        count that delegated to it would move every pending file; counting
        stands on its own path, so nothing moves.
        """
        self.q.seed({"001-a.md": TASK_BODY, "002-b.md": TASK_BODY}, {})
        real_fetch = DirectoryTaskProvider.fetch_pending

        def flipped(provider, claim=True, limit=None, owner=None):
            return real_fetch(provider, claim=claim, limit=limit, owner=owner)

        before = self.q.snapshot()
        with mock.patch.object(DirectoryTaskProvider, "fetch_pending", flipped):
            self.assertEqual(self.provider.count_pending(), 2)
        self.assertEqual(self.q.snapshot(), before)
        self.assertEqual(_names(self.q.claimed), [])


class ProviderDefaultCountTest(unittest.TestCase):
    """A source with no cheap count keeps the interface honest."""

    class CountingAdapter(TaskProvider):
        """Fetch is the only thing this adapter knows, so counting asks it."""

        def __init__(self):
            self.fetch_calls = 0

        def fetch_pending(self) -> list[Task]:
            self.fetch_calls += 1
            return [Task(id="a", body="A"), Task(id="b", body="B")]

    def test_default_count_asks_the_fetch(self):
        provider = self.CountingAdapter()
        self.assertEqual(provider.count_pending(), 2)
        self.assertEqual(provider.fetch_calls, 1)


class CountingProvider:
    """A provider that counts and treats a fetch as a test failure.

    Stands in for the real one on the generator's path: `fetch_pending` is the
    claim boundary, and T58's claim is that counting never crosses it, so a
    fetch here is a hard error rather than a silent move.
    """

    def __init__(self, count: int):
        self.count = count
        self.count_calls = 0
        self.fetch_calls = 0

    def count_pending(self) -> int:
        self.count_calls += 1
        return self.count

    def fetch_pending(self, *args, **kwargs) -> list[Task]:
        self.fetch_calls += 1
        raise AssertionError("the autonomous count fetched instead of counting")


class RejectingRunner:
    """Stands in for `SessionRunner`: every session comes back rejected.

    A rejected proposal ends the attempt before a proposal file or a queued
    task is written, so a whole `run()` stays a read of the queue.
    """

    def __init__(self):
        self.calls: list[object] = []

    def run(self, model, workdir, prompt, *, task_id=None, stage=None,
            notes="", **kwargs) -> SessionResult:
        self.calls.append(stage)
        output = f"## Summary\n\nVERDICT: {Verdict.FAIL.value}"
        return SessionResult(ok=True, verdict=Verdict.FAIL, peak_tokens=0,
                             duration_s=0.0, output=output,
                             out_file=Path(workdir) / "session.out")


class NoFetchDirectoryProvider(DirectoryTaskProvider):
    """A real directory provider whose fetch is a landmine.

    Used where the test wants real files on disk *and* proof about the calls:
    `count_pending()` is counted, `fetch_pending()` raises. A `run()` that
    finishes is therefore a run that never crossed the claim boundary, and the
    count tells the test the queue was actually read.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.count_calls = 0

    def count_pending(self) -> int:
        self.count_calls += 1
        return super().count_pending()

    def fetch_pending(self, *args, **kwargs) -> list[Task]:
        raise AssertionError("autonomous mode fetched instead of counting")


class AutonomousPendingCountTest(unittest.TestCase):
    """`AutonomousGenerator` counts; it never fetches."""

    def setUp(self):
        self.q = QueueFixture(self)

    def _generator(self, provider, target: int) -> AutonomousGenerator:
        return AutonomousGenerator(_cfg(self.q.root, target), RejectingRunner(),
                                   provider, log=self.q.lines.append)

    def test_pending_count_asks_the_provider_to_count_not_to_fetch(self):
        provider = CountingProvider(3)
        gen = self._generator(provider, target=5)
        self.assertEqual(gen._pending_count(), 3)
        self.assertEqual(provider.count_calls, 1)
        self.assertEqual(provider.fetch_calls, 0)

    def test_a_run_that_is_already_at_target_touches_nothing(self):
        """`run()` with the queue at target: zero added, no session, and both
        queue directories byte-identical."""
        self.q.seed({"001-a.md": TASK_BODY, "T04-parent.md": PARENT_BODY},
                    {"009-z.md": TASK_BODY})
        (self.q.claimed / "009-z.md.claim.json").write_text(
            '{"version": 1, "owner": "run-1", "claimed_at": 1.0}\n')
        provider = NoFetchDirectoryProvider(self.q.pending, self.q.claimed,
                                            log=self.q.lines.append)
        runner = RejectingRunner()
        gen = AutonomousGenerator(_cfg(self.q.root, 1), runner, provider,
                                  log=self.q.lines.append)
        before = self.q.snapshot()
        self.assertEqual(gen.run(self.q.root), 0)
        self.assertEqual(self.q.snapshot(), before)
        self.assertEqual(runner.calls, [], "a session ran at target")
        self.assertEqual(provider.count_calls, 2,
                         "the loop condition and the closing line are the "
                         "two counts a run at target makes")

    def test_the_loop_counts_every_attempt_and_never_fetches(self):
        """Below target the loop runs to its safety valve on rejected
        proposals. Every depth read is a `count_pending()`, the queue on disk
        is byte-identical throughout, and no fetch is ever attempted."""
        self.q.seed({"001-a.md": TASK_BODY}, {"009-z.md": TASK_BODY})
        provider = NoFetchDirectoryProvider(self.q.pending, self.q.claimed,
                                            log=self.q.lines.append)
        runner = RejectingRunner()
        gen = AutonomousGenerator(_cfg(self.q.root, 2), runner, provider,
                                  log=self.q.lines.append)
        before = self.q.snapshot()
        self.assertEqual(gen.run(self.q.root), 0)
        self.assertEqual(self.q.snapshot(), before)
        self.assertGreater(len(runner.calls), 0, "the loop never ran")
        self.assertGreater(provider.count_calls, 1,
                           "the loop ran without asking the queue its depth")

    def test_the_attempt_header_reports_the_count(self):
        """The loop condition and its log line read the same number, and the
        provider is asked rather than fetched."""
        provider = CountingProvider(1)
        runner = RejectingRunner()
        gen = AutonomousGenerator(_cfg(self.q.root, 2), runner, provider,
                                  log=self.q.lines.append)
        self.assertEqual(gen.run(self.q.root), 0)
        self.assertTrue(any("pending=1/2" in line for line in self.q.lines),
                        f"no attempt header reported the count: {self.q.lines}")
        self.assertEqual(provider.fetch_calls, 0)
        self.assertGreater(provider.count_calls, 1)


if __name__ == "__main__":
    unittest.main()
