"""Slice 5 — the spec §7 acceptance sweep for the single metadata record.

Everything earlier in the feature was proved per concern. This file asks the
two whole-system questions the spec's acceptance criteria pose, on a queue
that was written by the *old* code:

  * AC2 — a temp queue seeded with legacy sidecars (an orphan
    `claimed/N.md.claim.json`, a `pending/N.md.gh.json` whose markdown was
    claimed and left behind, a terminal dir's `gh.json`, and a task name whose
    slug differs from its stem): `status`, `board`, `requeue-claims` and an
    inbound sync pass read the correct linkage and ownership off those legacy
    shapes, and the queue converges to only new-format records;
  * AC4 — claim, terminal move and requeue run interleaved (deterministically,
    then concurrently across threads) leave, for every task id, at most one
    metadata record, and every record resolvable through the public API.

Also the residual grep gates, stated as an executable check: no legacy
sidecar name is derived or created anywhere in the production package outside
the migration reader (`harness/core/task_record.py` and the format vocabulary
in `harness/core/sync_sidecar.py`), and no caller still uses the retired
`resolve_linkage` / `claim_metadata.read_metadata` / `metadata_path` API.

All fixtures are temp dirs and fake API objects; no network, no container, no
live queue.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import Issue, IssueState, Label  # noqa: E402
from harness.cli import handlers  # noqa: E402
from harness.core import task_record  # noqa: E402
from harness.core.config import load  # noqa: E402
from harness.core.providers import DirectoryTaskProvider  # noqa: E402
from harness.core.stats import StatsStore  # noqa: E402
from harness.core.sync_inbound import InboundParams, run_inbound  # noqa: E402
from harness.core.sync_linkage import SyncLinkage  # noqa: E402
from harness.workflow.task_lifecycle import (  # noqa: E402
    CLAIMED_LOCATION,
    QUEUE_LOCATIONS_ALL,
    TaskLifecycle,
)
from tests.legacy_sidecars import (  # noqa: E402
    file_sidecar_path,
    task_dir_sidecar_path,
    write_legacy_claim,
    write_legacy_linkage,
)

REPO = "acme/widgets"
OWNER = "run-5555-aaaa"
PEER = "run-6666-bbbb"
STALE_HOURS = 48
THRESHOLD = 6.0

LEGACY_SUFFIXES = (".md.gh.json", ".md.claim.json", ".gh.json", ".claim.json")


def _issue(number, title, labels=("snes",), state=IssueState.OPEN):
    return Issue(number=number, title=title, body="issue body", state=state,
                 labels=tuple(Label(name) for name in labels),
                 html_url=f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """The inbound read surface: label/state-filtered issue listing."""

    def __init__(self, issues):
        self.issues = issues

    def list_issues(self, labels=(), state=IssueState.OPEN):
        wanted = set(labels)
        return [issue for issue in self.issues
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]


class _QueueRoot(unittest.TestCase):
    """A temp queue root, a provider and lifecycle over it, handlers wired to it."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="accept-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.queue = self.work / "queue"
        for sub in QUEUE_LOCATIONS_ALL:
            (self.queue / sub).mkdir(parents=True)
        cfg_path = self.work / "config.json"
        cfg_path.write_text(json.dumps({"workDir": str(self.work),
                                        "repoDir": str(self.work)}))
        self.cfg = load(cfg_path)
        self.messages: list[str] = []
        self.provider = DirectoryTaskProvider(self.queue / "pending",
                                              self.queue / CLAIMED_LOCATION,
                                              log=self.messages.append)
        self.lifecycle = TaskLifecycle(self.cfg, log=self.messages.append)
        wired = (self.cfg, StatsStore(self.work / "stats.jsonl"), None,
                 self.provider, None, lambda line="": self.messages.append(line))
        patcher = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- seeding -----------------------------------------------------------

    def seed_file(self, location: str, name: str, body: str = "task body") -> Path:
        path = self.queue / location / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def seed_dir(self, location: str, name: str) -> Path:
        task_dir = self.queue / location / name
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"id": name, "status": "active"}))
        (task_dir / "original.md").write_text("task body")
        return task_dir

    # -- the metadata view under test --------------------------------------

    def record_names(self) -> list[str]:
        meta = self.queue / task_record.META_DIR_NAME
        return sorted(p.name for p in meta.glob("*.json")) if meta.is_dir() \
            else []

    def legacy_paths(self) -> list[Path]:
        """Every legacy sidecar still sitting anywhere under the queue root."""
        found = []
        for path in self.queue.rglob("*"):
            if not path.is_file():
                continue
            if path.name == "gh.json" or any(
                    path.name.endswith(s) for s in LEGACY_SUFFIXES):
                found.append(path)
        return sorted(found)

    def metadata_keys(self) -> dict[str, list[Path]]:
        """Every metadata source on disk, grouped by the task key it names."""
        sources: dict[str, list[Path]] = {}
        for name in self.record_names():
            sources.setdefault(name[:-len(".json")], []).append(
                self.queue / task_record.META_DIR_NAME / name)
        for path in self.legacy_paths():
            key = self.legacy_key(path)
            if key:
                sources.setdefault(key, []).append(path)
        return sources

    @staticmethod
    def legacy_key(path: Path) -> str | None:
        """The task key a legacy sidecar names (its file-name key, slugged)."""
        if path.name == "gh.json":
            return task_record.task_key(path.parent.name)
        for suffix in LEGACY_SUFFIXES:
            if path.name.endswith(suffix):
                return task_record.task_key(path.name[:-len(suffix)])
        return None

    def assert_one_record_per_task(self):
        """AC4: no task id is described by more than one metadata source."""
        duplicates = {key: [str(p.relative_to(self.queue)) for p in paths]
                      for key, paths in self.metadata_keys().items()
                      if len(paths) > 1}
        self.assertEqual({}, duplicates,
                         "a task ended up with more than one metadata record")

    def assert_every_record_resolvable(self):
        """AC4: every record on disk reads back through the public API.

        A record is resolvable when `read_record` returns exactly what the
        document holds, and when the task it describes is either still in the
        queue or reported as an orphan claim — never silently unreachable.
        """
        for name in self.record_names():
            key = name[:-len(".json")]
            path = self.queue / task_record.META_DIR_NAME / name
            payload = json.loads(path.read_text())
            self.assertEqual(task_record.RECORD_SCHEMA_VERSION,
                             payload["version"], f"{name} is not new-schema")
            record = task_record.read_record(self.queue, key)
            self.assertEqual(payload["github"] is not None,
                             record.github is not None, key)
            self.assertEqual(payload["claim"] is not None,
                             record.claim is not None, key)
            if record.github is not None:
                self.assertEqual(record.github.issue,
                                 task_record.read_linkage(self.queue, key).issue)
            if record.claim is not None and not self.task_exists(key):
                reported = {task_record.task_key(o.task_id)
                            for o in task_record.list_orphan_claims(self.queue)}
                self.assertIn(key, reported,
                              "a claim record with no task is neither a claim "
                              "nor a reported orphan")

    def task_exists(self, key: str) -> bool:
        """True when a task file or task dir slugging to `key` is in a location."""
        for location in QUEUE_LOCATIONS_ALL:
            directory = self.queue / location
            for entry in directory.iterdir() if directory.is_dir() else []:
                if entry.name.startswith("."):
                    continue
                if entry.suffix == ".md" and task_record.task_key(
                        entry.stem) == key:
                    return True
                if entry.is_dir() and task_record.task_key(entry.name) == key:
                    return True
        return False

    def run_inbound(self, issues) -> int:
        report = run_inbound(FakeApi(issues), InboundParams(
            queue_dir=self.queue, repo=REPO, log=self.messages.append))
        return report.imported

    def capture(self, command, *args, **kwargs) -> str:
        """Run one handler and return everything it reported.

        Report output goes to the log sink handed to `handlers.build` (what
        `status` prints) or straight to stdout (what `board` prints), so both
        are collected here.
        """
        mark = len(self.messages)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(0, command(*args, **kwargs))
        return "\n".join(self.messages[mark:] + [buf.getvalue()])


class SeededLegacyQueueTest(_QueueRoot):
    """AC2: read a pre-record queue correctly, then converge to records."""

    def setUp(self):
        super().setUp()
        self.stale = time.time() - STALE_HOURS * 3600
        # 1. A claimed task whose linkage sidecar stayed in `pending/` when
        #    the claim moved only the markdown — the orphan the record removes
        #    — under a name whose slug differs from its stem.
        self.spaced = "006 spaced legacy"
        claimed = self.seed_file(CLAIMED_LOCATION, self.spaced)
        write_legacy_linkage(
            file_sidecar_path(self.queue / "pending" / f"{self.spaced}.md"),
            SyncLinkage(issue=41, repo=REPO, comment_ids={"handoff-1": "c-1"},
                        demo=True))
        write_legacy_claim(claimed, OWNER, self.stale)
        # The claim is 48h old on the record *and* on the disk clock: the
        # requeue command ages a claim off its file mtime.
        os.utime(claimed, (self.stale, self.stale))
        # 2. The `002-…` defect: a claim sidecar whose markdown is gone.
        self.orphan = "002-live-instruction-injection"
        write_legacy_claim(self.queue / CLAIMED_LOCATION / f"{self.orphan}.md",
                           PEER, self.stale)
        # 3. A waiting task with its linkage beside it.
        self.seed_file("pending", "008-waiting")
        write_legacy_linkage(
            file_sidecar_path(self.queue / "pending" / "008-waiting.md"),
            SyncLinkage(issue=42, repo=REPO))
        # 4. A terminal task dir carrying its own `gh.json`.
        self.seed_dir("done", "009-finished")
        write_legacy_linkage(
            task_dir_sidecar_path(self.queue / "done" / "009-finished"),
            SyncLinkage(issue=43, repo=REPO))

    # -- (a) the legacy shapes are read correctly --------------------------

    def test_ownership_and_linkage_read_off_the_legacy_shapes(self):
        claims = self.provider.list_owned_claims()
        self.assertEqual([OWNER], [c.owner for c in claims])
        self.assertEqual(self.spaced, Path(claims[0].filename).stem)
        self.assertAlmostEqual(self.stale, claims[0].claimed_at, places=3)

        self.assertEqual(41, task_record.read_linkage(
            self.queue, self.spaced).issue)
        self.assertEqual(42, task_record.read_linkage(
            self.queue, "008-waiting").issue)
        self.assertEqual(43, task_record.read_linkage(
            self.queue, "009-finished").issue)
        self.assertEqual({"handoff-1": "c-1"}, task_record.read_linkage(
            self.queue, self.spaced).comment_ids)

    def test_the_demo_flag_survives_the_legacy_sidecar(self):
        tasks = self.provider.list_claims()
        self.assertEqual([True], [t.meta.get("demo") for t in tasks])

    def test_status_and_board_report_the_seeded_queue(self):
        out = self.capture(handlers.cmd_status)
        self.assertIn(task_record.task_key(self.spaced), out)
        self.assertNotIn(self.orphan, out,
                         "an orphan claim surfaced as a task")
        board = self.capture(handlers.cmd_board)
        self.assertIn(f"owner={OWNER}", board)
        self.assertNotIn(self.orphan, board)

    def test_orphan_claim_is_reported_but_never_a_task(self):
        orphans = task_record.list_orphan_claims(self.queue)
        self.assertEqual([self.orphan], [o.task_id for o in orphans])
        self.assertEqual(PEER, orphans[0].owner)
        self.assertEqual(1, self.provider.count_pending())
        self.assertEqual([task_record.task_key(self.spaced)],
                         [t.id for t in self.provider.list_claims()])

    def test_inbound_sync_matches_by_legacy_linkage_not_title(self):
        # Titles match nothing: only the legacy linkage can prevent a
        # duplicate import (FR-1.6 precedence, FR-B3).
        imported = self.run_inbound([
            _issue(41, "Completely different title"),
            _issue(42, "Another unrelated name"),
            _issue(43, "Terminal task under a new name"),
        ])
        self.assertEqual(0, imported)
        self.assertEqual([], [p.name for p in
                              (self.queue / "pending").glob("Completely*")])
        self.assertEqual("task body",
                         (self.queue / "pending" / "008-waiting.md").read_text())

    # -- (b) convergence to only new-format records ------------------------

    def test_a_sync_pass_migrates_every_sidecar_it_reads(self):
        self.run_inbound([_issue(41, "Completely different title"),
                          _issue(42, "Another unrelated name"),
                          _issue(43, "Terminal task under a new name")])
        self.assertEqual(
            ["006_spaced_legacy", "008-waiting", "009-finished"],
            [n[:-len(".json")] for n in self.record_names()])
        # Every sidecar belonging to a task in the queue was read, migrated
        # and retired. The one left is the orphan claim sidecar: no task read
        # sights it, so the hygiene pass (FR-E5) is its cleanup path.
        self.assertEqual([f"{CLAIMED_LOCATION}/{self.orphan}.md.claim.json"],
                         [str(p.relative_to(self.queue))
                          for p in self.legacy_paths()])
        self.provider.sweep_legacy_metadata()
        self.assertEqual([], self.legacy_paths())

    def test_requeue_claims_migrates_requeues_and_cleans_the_orphan(self):
        self.assertEqual(0, handlers.cmd_requeue_claims(older_than=THRESHOLD))
        logged = " | ".join(self.messages)
        self.assertIn(f"requeued {task_record.task_key(self.spaced)}", logged)
        self.assertIn(f"cleaned orphan claim record {self.orphan}", logged)
        self.assertEqual([], self.legacy_paths())
        self.assertEqual([f"{self.spaced}.md", "008-waiting.md"],
                         sorted(p.name for p in
                                (self.queue / "pending").glob("*.md")))
        record = task_record.read_record(self.queue, self.spaced)
        self.assertIsNone(record.claim, "the handed-back claim still named an owner")
        self.assertEqual(41, record.github.issue,
                         "the linkage did not follow the handed-back task")
        self.assertFalse(task_record.record_path(self.queue, self.orphan)
                         .exists())

    def test_running_every_command_twice_converges(self):
        issues = [_issue(41, "Completely different title"),
                  _issue(42, "Another unrelated name")]
        for _ in range(2):
            self.capture(handlers.cmd_status)
            self.capture(handlers.cmd_board)
            self.run_inbound(issues)
            self.capture(handlers.cmd_requeue_claims, older_than=THRESHOLD)
        self.assertEqual([], self.legacy_paths(),
                         "a legacy sidecar survived two full passes")
        self.assert_one_record_per_task()
        self.assert_every_record_resolvable()
        # The second pass wrote nothing new: the store is byte-identical.
        before = {p.name: p.read_text() for p in
                  (self.queue / task_record.META_DIR_NAME).glob("*.json")}
        self.capture(handlers.cmd_requeue_claims, older_than=THRESHOLD)
        after = {p.name: p.read_text() for p in
                 (self.queue / task_record.META_DIR_NAME).glob("*.json")}
        self.assertEqual(before, after)

    def test_no_legacy_file_is_recreated_after_migration(self):
        """FR-E3: once migrated, the record is the only thing ever written."""
        self.capture(handlers.cmd_requeue_claims, older_than=THRESHOLD)
        self.assertEqual([], self.legacy_paths())
        for _ in range(2):
            self.capture(handlers.cmd_status)
            self.capture(handlers.cmd_board)
            self.run_inbound([_issue(41, "Completely different title"),
                              _issue(42, "Another unrelated name")])
            self.capture(handlers.cmd_requeue_claims, older_than=THRESHOLD)
        self.assertEqual([], self.legacy_paths())


class InterleavedTransitionTest(_QueueRoot):
    """AC4 (deterministic): claim, requeue-with-collision, intake, terminal move."""

    def setUp(self):
        super().setUp()
        for name, issue in (("050-alpha", 50), ("051-beta", 51),
                            ("052-gamma", 52)):
            self.seed_file("pending", name)
            write_legacy_linkage(
                file_sidecar_path(self.queue / "pending" / f"{name}.md"),
                SyncLinkage(issue=issue, repo=REPO))

    def test_interleaved_transitions_leave_one_resolvable_record(self):
        claimed = self.provider.fetch_pending(claim=True, limit=2, owner=OWNER)
        self.assertEqual(["050-alpha", "051-beta"], [t.id for t in claimed])
        self.assert_one_record_per_task()

        # A requeue onto a pending name that already exists takes the
        # `-requeued` suffix: the record follows the task, the old key goes (§5.2).
        self.seed_file("pending", "050-alpha", "another task with the same name")
        self.provider.requeue_claim("050-alpha", owner=OWNER)
        self.assertEqual(50, task_record.read_linkage(
            self.queue, "050-alpha-requeued").issue)
        self.assertFalse(task_record.record_path(self.queue, "050-alpha").exists(),
                         "the old key was stranded beside the renamed task")
        self.assert_one_record_per_task()

        # Intake then release: the staging markdown goes, the record stays.
        beta = next(t for t in self.provider.list_claims() if t.id == "051-beta")
        self.lifecycle.intake(beta)
        self.provider.release_claim(beta)
        self.assertEqual(51, task_record.read_linkage(self.queue, "051-beta").issue)
        self.assertIsNone(task_record.read_record(self.queue, "051-beta").claim)
        self.assert_one_record_per_task()

        # Terminal moves from a task dir and from a task file.
        self.lifecycle.complete("051-beta", "shipped")
        self.lifecycle.park("052-gamma", "operator park", from_="pending")
        self.assertEqual(51, task_record.read_linkage(self.queue, "051-beta").issue)
        self.assertEqual(52, task_record.read_linkage(self.queue, "052-gamma").issue)
        self.assertEqual([], self.legacy_paths())
        self.assert_one_record_per_task()
        self.assert_every_record_resolvable()

    def test_a_claim_rolled_back_leaves_no_owned_record(self):
        """§5.1 through the real provider: a failed record write rolls back."""
        blocked = self.queue / task_record.META_DIR_NAME
        blocked.write_text("not a directory")
        with self.assertRaises(Exception):
            self.provider.fetch_pending(claim=True, limit=1, owner=OWNER)
        # The claim was rolled back, so the queue holds exactly what it did:
        # nothing taken, nothing half-taken.
        self.assertEqual(["050-alpha.md", "051-beta.md", "052-gamma.md"],
                         sorted(p.name for p in (self.queue / "pending").glob("*.md")))
        self.assertEqual([], self.provider.list_owned_claims())
        self.assertEqual([], self.legacy_paths() and
                         [p for p in self.legacy_paths()
                          if p.name.endswith(".claim.json")])


class ConcurrentTransitionTest(_QueueRoot):
    """AC4 (concurrent): claim + terminal move + requeue racing on one queue."""

    def setUp(self):
        super().setUp()
        self.ids = [f"06{i}-race" for i in range(6)]
        for index, name in enumerate(self.ids):
            self.seed_file("pending", name, f"body {name}")
            write_legacy_linkage(
                file_sidecar_path(self.queue / "pending" / f"{name}.md"),
                SyncLinkage(issue=60 + index, repo=REPO))
        self.conflicts = 0
        self.errors: list[BaseException] = []

    def _guard(self, action) -> None:
        """Run one transition; a lost race (OSError) is a conflict, not a bug."""
        try:
            action()
        except OSError:
            self.conflicts += 1
        except BaseException as exc:      # noqa: BLE001 - reported, never swallowed
            self.errors.append(exc)

    def _claim(self):
        self._guard(lambda: self.provider.fetch_pending(
            claim=True, limit=1, owner=OWNER))

    def _requeue(self):
        def action():
            claims = self.provider.list_owned_claims()
            if claims:
                self.provider.requeue_claim(claims[0].filename, owner=OWNER)
        self._guard(action)

    def _finish(self):
        def action():
            tasks = self.provider.list_claims()
            if not tasks:
                return
            task = tasks[0]
            self.lifecycle.intake(task)
            self.provider.release_claim(task)
            self.lifecycle.complete(task.id, "shipped")
        self._guard(action)

    def _relink(self):
        def action():
            for index, name in enumerate(self.ids):
                task_record.write_linkage(
                    self.queue, name,
                    SyncLinkage(issue=60 + index, repo=REPO,
                                comment_ids={"evt": f"c-{index}"}))
        self._guard(action)

    def test_racing_transitions_leave_one_record_per_task(self):
        workers = ([self._claim] * 2 + [self._requeue] + [self._finish]
                   + [self._relink])
        stop = time.time() + 0.6
        threads = [threading.Thread(target=self._run_until, args=(w, stop))
                   for w in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], self.errors)
        self.assertLessEqual(1, self.conflicts,
                             "the workers never actually raced each other")
        self.assert_one_record_per_task()
        # The hygiene pass retires whatever a racing read had not sighted yet;
        # nothing may be left describing a task twice (FR-E4/FR-E5).
        self.provider.sweep_legacy_metadata()
        self.assertEqual([], self.legacy_paths())
        self.assert_one_record_per_task()
        self.assert_every_record_resolvable()

    def _run_until(self, worker, stop: float) -> None:
        while time.time() < stop:
            worker()

    def test_two_tasks_never_share_a_record_after_a_requeue_collision(self):
        """§5.9 under a race: `X` and `X-requeued` keep separate records."""
        self.seed_file("pending", "070-dup")
        self.provider.fetch_pending(claim=True, owner=OWNER)
        task_record.write_linkage(self.queue, "070-dup",
                                  SyncLinkage(issue=70, repo=REPO))
        self.seed_file("pending", "070-dup", "a different task, same name")
        self.provider.requeue_claim("070-dup", owner=OWNER)
        self.assertEqual(70, task_record.read_linkage(
            self.queue, "070-dup-requeued").issue)
        self.assertIsNone(task_record.read_record(self.queue, "070-dup").claim)
        self.assert_one_record_per_task()


class LegacyDerivationGateTest(unittest.TestCase):
    """AC3/AC5 as an executable gate over the production package."""

    RETIRED_API = ("resolve_linkage", "read_metadata(", "write_metadata(",
                   "remove_metadata(", "metadata_path(",
                   "move_sidecar_into_task_dir")
    MIGRATION_MODULES = {"harness/core/task_record.py",
                         "harness/core/sync_sidecar.py"}

    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.sources = sorted(
            p for package in ("harness", "external")
            for p in (self.root / package).rglob("*.py"))

    def _lines(self, path: Path, needle: str) -> list[int]:
        return [number for number, line in enumerate(
            path.read_text().splitlines(), start=1)
            if needle in line and not line.lstrip().startswith("#")]

    def test_no_legacy_sidecar_name_is_derived_outside_the_migration_reader(self):
        offenders = {}
        for path in self.sources:
            relative = str(path.relative_to(self.root))
            if relative in self.MIGRATION_MODULES:
                continue
            hits = [n for needle in ('".gh.json', '".claim.json', "'gh.json'",
                                     "'.claim.json")
                    for n in self._lines(path, needle)]
            if hits:
                offenders[relative] = hits
        self.assertEqual({}, offenders,
                         "a module outside the migration reader derives a "
                         "metadata path from a task-file name")

    def test_no_caller_uses_the_retired_sidecar_api(self):
        offenders = {}
        for path in self.sources:
            relative = str(path.relative_to(self.root))
            hits = [n for needle in self.RETIRED_API
                    for n in self._lines(path, needle)]
            if hits:
                offenders[relative] = hits
        self.assertEqual({}, offenders,
                         "a caller still uses the retired sidecar API")

    def test_the_migration_reader_is_the_only_importer_of_the_legacy_format(self):
        importers = {}
        for path in self.sources:
            relative = str(path.relative_to(self.root))
            hits = self._lines(path, "sync_sidecar")
            if hits and relative != "harness/core/sync_sidecar.py":
                importers[relative] = hits
        self.assertEqual({"harness/core/task_record.py"},
                         set(importers),
                         "a module outside the migration reader imports the "
                         "legacy sidecar format")


if __name__ == "__main__":
    unittest.main()
