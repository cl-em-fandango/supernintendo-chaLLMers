"""T39: table tests for the stats report (finding F11).

`harness.py report` is the only thing the operator actually reads, and nothing
pinned what it computes. The aggregation functions in
`harness/core/stats.py` are pure — `list[dict]` in, `list[dict]` out — so every
number in the report can be hand-computed in the test and asserted exactly
(`assert m["sessions"] == 4`, never `> 0`).

The fixture is a *synthetic* 10-row slice of the row schema, not a copy of the
live store: the live JSONL grows on every run, so a test that read it would be
green today and red tomorrow. The fixture keeps the shape the live rows have
(two models, three stages, the verdict mix the real data carries) and the
expected numbers are written out as literals next to the arithmetic that
produces them, so a changed formula shows up as a diff against the comment
rather than as a mystery failure.

Two behaviours are pinned *as they are*, not as they should be:
- a `reject` verdict is stored with outcome `unknown` (pinned in
  `tests/test_pi_verdict.py`), so `stage_report` does not count it as a bounce.
  The card that owns the verdict->outcome mapping is T20, so the case that
  wants the other answer is `@expectedFailure` naming T20 rather than a fix.
- `_pct` re-rounds an already-rounded rate, so a 1/6 error rate renders `17%`.

Nothing here opens the operator's stats directory. Every path the store tests
use is built under `tempfile.mkdtemp()`, and one case asserts exactly that.

Run from the repo root:  python3 -m unittest tests.test_stats
"""
from __future__ import annotations

import dataclasses
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.stats import (
    SessionRecord,
    StatsStore,
    _group,
    _pct,
    model_report,
    render_report,
    stage_report,
    task_report,
)

# A 32k-window model's ceiling (`Config.model_context` for a `*32k` name is
# 32 * 1024). One fixture row peaks exactly here: an over-cap session is the
# row the report must survive without a division or a format blowing up.
PEAK_TOKENS_CAP = 32_768

MODEL_ALPHA = "alpha-model"
MODEL_BETA = "beta-model"


def row(**kw) -> dict:
    """One complete stats row with sensible defaults, overridden by `kw`.

    The keys are exactly the JSONL schema `StatsStore.all()` returns, because
    the analytics take the store's read shape (plain dicts), not the
    `SessionRecord` dataclass. `test_fixture_matches_the_record_schema` keeps
    the two in step.
    """
    base = {
        "ts": "2026-08-26T00:00:00+0000",
        "task_id": "t1",
        "stage": "spec_author",
        "model": MODEL_ALPHA,
        "verdict": "pass",
        "outcome": "pass",
        "peak_tokens": 1000,
        "duration_s": 100.0,
        "rc": 0,
        "prompt_chars": 1000,
        "slice": None,
        "iteration": 1,
        "session_file": None,
        "notes": "",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# The fixture
#
#   row  model  stage            verdict   outcome   rc  dur_s  peak    task
#   ---- ------ ---------------- --------- --------- --  -----  ------  ----
#   A1   alpha  spec_author      pass      pass       0  100.0   1000   t1
#   A2   alpha  slice_implement  done      done       0  200.0   2000   t1
#   A3   alpha  slice_implement  error     error      1  300.0   3000   t2
#   A4   alpha  tech_review      unknown   unknown    0    0.0  32768   t2
#   B1   beta   spec_author      reject    unknown    0  400.0   4000   t2
#   B2   beta   slice_implement  pass      pass       0  500.0   5000   t3
#   B3   beta   slice_implement  kickback  kickback   0  600.0   6000   t3
#   B4   beta   tech_review      done      done       0  700.0   7000   None
#   B5   beta   tech_review      error     error      0  800.0   8000   t3
#   B6   beta   spec_author      fail      fail       0  900.0   9000   t1
#
# `reject` (B1) and `kickback` (B3) are the discriminating pair: the first is
# stored as outcome `unknown`, the second as `kickback`, so they land on
# different sides of every "decided" filter in the report.
ROWS: list[dict] = [
    row(),
    row(stage="slice_implement", verdict="done", outcome="done",
        duration_s=200.0, peak_tokens=2000),
    row(stage="slice_implement", verdict="error", outcome="error",
        rc=1, duration_s=300.0, peak_tokens=3000, task_id="t2"),
    row(stage="tech_review", verdict="unknown", outcome="unknown",
        duration_s=0.0, peak_tokens=PEAK_TOKENS_CAP, task_id="t2"),
    row(model=MODEL_BETA, verdict="reject", outcome="unknown",
        duration_s=400.0, peak_tokens=4000, task_id="t2"),
    row(model=MODEL_BETA, stage="slice_implement", duration_s=500.0,
        peak_tokens=5000, task_id="t3"),
    row(model=MODEL_BETA, stage="slice_implement", verdict="kickback",
        outcome="kickback", duration_s=600.0, peak_tokens=6000, task_id="t3"),
    row(model=MODEL_BETA, stage="tech_review", verdict="done", outcome="done",
        duration_s=700.0, peak_tokens=7000, task_id=None),
    row(model=MODEL_BETA, stage="slice_implement", verdict="error",
        outcome="error", duration_s=800.0, peak_tokens=8000, task_id="t3"),
    row(model=MODEL_BETA, verdict="fail", outcome="fail",
        duration_s=900.0, peak_tokens=9000),
]


def by_model(name: str) -> dict:
    """The `model_report` row for `name` (fails loudly if it is absent)."""
    matches = [m for m in model_report(ROWS) if m["model"] == name]
    assert len(matches) == 1, f"{name!r} missing from model_report"
    return matches[0]


def by_stage(name: str) -> dict:
    matches = [s for s in stage_report(ROWS) if s["stage"] == name]
    assert len(matches) == 1, f"{name!r} missing from stage_report"
    return matches[0]


def by_task(name: str) -> dict:
    matches = [t for t in task_report(ROWS) if t["task_id"] == name]
    assert len(matches) == 1, f"{name!r} missing from task_report"
    return matches[0]


def rendered_line(text: str, prefix: str) -> str:
    """The single rendered report line that starts with `prefix`."""
    matches = [ln for ln in text.splitlines() if ln.startswith(prefix)]
    assert len(matches) == 1, f"{prefix!r} matched {len(matches)} lines"
    return matches[0]


class FixtureTest(unittest.TestCase):
    """The fixture itself: a fixture that quietly shrinks tests nothing."""

    def test_fixture_matches_the_record_schema(self):
        # The analytics take dicts; the store writes `SessionRecord`. If either
        # side grows a field, this fails and the fixture gets updated with it.
        fields = {f.name for f in dataclasses.fields(SessionRecord)}
        self.assertEqual(set(row().keys()), fields)

    def test_fixture_covers_the_required_shapes(self):
        self.assertEqual(len(ROWS), 10)
        self.assertEqual({r["model"] for r in ROWS}, {MODEL_ALPHA, MODEL_BETA})
        self.assertEqual({r["stage"] for r in ROWS},
                         {"spec_author", "slice_implement", "tech_review"})
        self.assertEqual({r["verdict"] for r in ROWS},
                         {"pass", "done", "error", "unknown", "reject",
                          "kickback", "fail"})
        self.assertEqual([r for r in ROWS if r["rc"] != 0], [ROWS[2]])
        self.assertEqual([r for r in ROWS if r["duration_s"] == 0], [ROWS[3]])
        at_cap = [r for r in ROWS if r["peak_tokens"] == PEAK_TOKENS_CAP]
        self.assertEqual(at_cap, [ROWS[3]])
        self.assertEqual([r["task_id"] for r in ROWS if r["task_id"] is None],
                         [None])

    def test_fixture_rows_are_independent_dicts(self):
        # `row()` must hand out a fresh dict every call, or one override would
        # mutate the shared fixture and every later case would read it.
        a = row()
        a["model"] = "mutated"
        self.assertEqual(row()["model"], MODEL_ALPHA)


class ModelReportTest(unittest.TestCase):
    """`model_report`: per-model counts, rates and speed, hand-computed."""

    def test_groups_are_sorted_and_complete(self):
        report = model_report(ROWS)
        self.assertEqual([m["model"] for m in report],
                         [MODEL_ALPHA, MODEL_BETA])
        # 4 alpha rows + 6 beta rows == the whole fixture.
        self.assertEqual(sum(m["sessions"] for m in report), len(ROWS))

    def test_alpha_counts(self):
        # alpha rows are A1..A4: pass, done, error, unknown.
        # decided = outcome not in (error, unknown) -> A1, A2 -> 2
        # rejections = decided and in (kickback, fail, kickout) -> none -> 0
        # errors = every row with outcome error (decided or not) -> A3 -> 1
        m = by_model(MODEL_ALPHA)
        self.assertEqual(m["sessions"], 4)
        self.assertEqual(m["rejections"], 0)

    def test_alpha_rates(self):
        m = by_model(MODEL_ALPHA)
        self.assertEqual(m["rejection_rate"], 0.0)      # 0 / 2 decided
        self.assertEqual(m["pass_rate"], 1.0)           # 2 / 2 decided
        self.assertEqual(m["error_rate"], 0.25)         # 1 / 4 all rows

    def test_alpha_speed(self):
        m = by_model(MODEL_ALPHA)
        # (100.0 + 200.0 + 300.0 + 0.0) / 4 = 150.0
        self.assertEqual(m["avg_duration_s"], 150.0)
        # (1000 + 2000 + 3000 + 32768) / 4 = 9692
        self.assertEqual(m["avg_peak_tokens"], 9692)
        self.assertEqual(m["total_tokens"], 38768)

    def test_beta_counts(self):
        # beta rows are B1..B6: unknown(reject), pass, kickback, done, error,
        # fail. decided -> B2, B3, B4, B6 -> 4; rejections -> B3, B6 -> 2.
        m = by_model(MODEL_BETA)
        self.assertEqual(m["sessions"], 6)
        self.assertEqual(m["rejections"], 2)

    def test_beta_rates(self):
        m = by_model(MODEL_BETA)
        self.assertEqual(m["rejection_rate"], 0.5)      # 2 / 4 decided
        self.assertEqual(m["pass_rate"], 0.5)           # B2 + B4 / 4 decided
        self.assertEqual(m["error_rate"], 0.167)        # 1 / 6, round(.,3)

    def test_beta_speed(self):
        m = by_model(MODEL_BETA)
        # (400 + 500 + 600 + 700 + 800 + 900) / 6 = 650.0
        self.assertEqual(m["avg_duration_s"], 650.0)
        # (4000 + 5000 + 6000 + 7000 + 8000 + 9000) / 6 = 6500
        self.assertEqual(m["avg_peak_tokens"], 6500)
        self.assertEqual(m["total_tokens"], 39000)

    def test_empty_rows_produce_no_report_rows(self):
        self.assertEqual(model_report([]), [])


class StageReportTest(unittest.TestCase):
    """`stage_report`: how often work gets bounced at each stage."""

    def test_stage_counts(self):
        report = stage_report(ROWS)
        self.assertEqual([s["stage"] for s in report],
                         ["slice_implement", "spec_author", "tech_review"])
        self.assertEqual([s["sessions"] for s in report], [5, 3, 2])

    def test_bounce_counts_and_rates(self):
        # slice_implement: A2 done, A3 error, B2 pass, B3 kickback, B5 error
        #                  -> 1 bounce of 5 -> 0.2
        self.assertEqual(by_stage("slice_implement")["bounces"], 1)
        self.assertEqual(by_stage("slice_implement")["bounce_rate"], 0.2)
        # spec_author: A1 pass, B1 reject(->unknown), B6 fail -> 1 of 3
        self.assertEqual(by_stage("spec_author")["bounces"], 1)
        self.assertEqual(by_stage("spec_author")["bounce_rate"], 0.333)
        # tech_review: A4 unknown, B4 done -> no bounce at all
        self.assertEqual(by_stage("tech_review")["bounces"], 0)
        self.assertEqual(by_stage("tech_review")["bounce_rate"], 0.0)

    def test_speed_columns(self):
        # spec_author: (100 + 400 + 900) / 3 = 466.666 -> 466.7
        self.assertEqual(by_stage("spec_author")["avg_duration_s"], 466.7)
        # spec_author tokens: (1000 + 4000 + 9000) / 3 = 4666.66 -> int 4666
        self.assertEqual(by_stage("spec_author")["avg_peak_tokens"], 4666)
        # slice_implement: (200+300+500+600+800)/5 = 480.0, tokens 24000/5=4800
        self.assertEqual(by_stage("slice_implement")["avg_duration_s"], 480.0)
        self.assertEqual(by_stage("slice_implement")["avg_peak_tokens"], 4800)
        # tech_review: (0 + 700) / 2 = 350.0, tokens (32768+7000)/2 = 19884
        self.assertEqual(by_stage("tech_review")["avg_duration_s"], 350.0)
        self.assertEqual(by_stage("tech_review")["avg_peak_tokens"], 19884)

    def test_reject_row_is_not_counted_as_a_bounce_today(self):
        # Pinned current behaviour. A `reject` verdict is stored with outcome
        # `unknown` (see `tests/test_pi_verdict.py`), so it is invisible to the
        # bounce filter: a stage whose only row is a reject reports one session
        # and no bounce at all. In the full fixture the same blindness is why
        # `spec_author` sees 3 sessions and 1 bounce (the `fail`, not the
        # `reject`) - see `test_bounce_counts_and_rates`.
        rows = [row(stage="spec_author", verdict="reject", outcome="unknown")]
        report = stage_report(rows)
        self.assertEqual(report[0]["sessions"], 1)
        self.assertEqual(report[0]["bounces"], 0)
        self.assertEqual(report[0]["bounce_rate"], 0.0)

    @unittest.expectedFailure
    def test_reject_row_should_count_as_a_bounce(self):
        # What the number *means* is a bounce: the reviewer rejected the work.
        # The verdict->outcome mapping that hides it belongs to T20
        # (plan-2026-08-26-done/T20-unknown-vs-crash.md, "unknown vs crash"),
        # which is out of scope for this card, so this case is expected to fail
        # until that mapping changes. Do not "fix" it here.
        rows = [row(stage="spec_author", verdict="reject", outcome="unknown")]
        self.assertEqual(stage_report(rows)[0]["bounces"], 1)


class TaskReportTest(unittest.TestCase):
    """`task_report`: totals per task, including the rows with no task."""

    def test_totals_per_task(self):
        report = task_report(ROWS)
        # `_group` stringifies the key, so the None row sorts first ("None").
        self.assertEqual([t["task_id"] for t in report],
                         ["None", "t1", "t2", "t3"])
        # t1 = A1 + A2 + B6: 1000+2000+9000 tokens, 100+200+900 s, one fail
        self.assertEqual(by_task("t1")["sessions"], 3)
        self.assertEqual(by_task("t1")["total_tokens"], 12000)
        self.assertEqual(by_task("t1")["total_duration_s"], 1200.0)
        self.assertEqual(by_task("t1")["bounces"], 1)
        # t2 = A3 + A4 + B1: 3000+32768+4000 tokens, 300+0+400 s, no bounce
        self.assertEqual(by_task("t2")["sessions"], 3)
        self.assertEqual(by_task("t2")["total_tokens"], 39768)
        self.assertEqual(by_task("t2")["total_duration_s"], 700.0)
        self.assertEqual(by_task("t2")["bounces"], 0)
        # t3 = B2 + B3 + B5: 19000 tokens, 1900.0 s, one kickback
        self.assertEqual(by_task("t3")["sessions"], 3)
        self.assertEqual(by_task("t3")["total_tokens"], 19000)
        self.assertEqual(by_task("t3")["total_duration_s"], 1900.0)
        self.assertEqual(by_task("t3")["bounces"], 1)

    def test_task_id_none_is_grouped_and_does_not_raise(self):
        # Autonomous runs have no task. They must land in one group, not vanish
        # and not blow up on the sort (`None` is not orderable with `str`).
        group = by_task("None")
        self.assertEqual(group["sessions"], 1)
        self.assertEqual(group["total_tokens"], 7000)
        self.assertEqual(group["total_duration_s"], 700.0)
        self.assertEqual(group["bounces"], 0)
        # Nothing is dropped on the way through the grouping.
        self.assertEqual(sum(g["sessions"] for g in task_report(ROWS)),
                         len(ROWS))

    def test_two_task_id_none_rows_aggregate_into_one_group(self):
        rows = ROWS + [row(task_id=None, stage="holistic", verdict="done",
                           outcome="done", peak_tokens=500, duration_s=50.0)]
        groups = task_report(rows)
        none_groups = [t for t in groups if t["task_id"] == "None"]
        self.assertEqual(len(none_groups), 1)
        self.assertEqual(none_groups[0]["sessions"], 2)
        self.assertEqual(none_groups[0]["total_tokens"], 7500)
        self.assertEqual(none_groups[0]["total_duration_s"], 750.0)

    def test_missing_task_id_key_groups_as_unknown(self):
        # `_group` defaults a *missing* key to the string "unknown", which is a
        # different bucket from a present-but-None key. Both are reportable.
        rows = [{k: v for k, v in row().items() if k != "task_id"}]
        self.assertEqual(task_report(rows)[0]["task_id"], "unknown")
        self.assertEqual(_group(rows, "task_id"), {"unknown": rows})


class PctTest(unittest.TestCase):
    """`_pct`: the contract is a fixed-width string, pinned exactly."""

    def test_none_is_two_spaces_and_a_dash(self):
        self.assertEqual(_pct(None), "  -")

    def test_zero(self):
        self.assertEqual(_pct(0), "0%")

    def test_one(self):
        self.assertEqual(_pct(1), "100%")

    def test_one_half(self):
        self.assertEqual(_pct(0.5), "50%")

    def test_re_rounds_an_already_rounded_rate(self):
        # 1/6 -> round(...,3) = 0.167 -> 16.7 -> "%.0f" -> "17%". The report
        # column is therefore one percent away from the true 16.67; pinned so
        # nobody reads the column as exact.
        self.assertEqual(_pct(round(1 / 6, 3)), "17%")


class RenderReportTest(unittest.TestCase):
    """`render_report`: the aggregation and the rendered columns must agree."""

    def test_empty_data_returns_a_string(self):
        text = render_report([])
        self.assertIsInstance(text, str)
        self.assertIn("=== Session stats (0 total) ===", text)
        # Every section header still renders with nothing to aggregate.
        self.assertIn("--- By model (quality & speed) ---", text)
        self.assertIn("--- By stage (bounce rate) ---", text)
        self.assertIn("--- By task ---", text)

    def test_contains_every_model_name(self):
        text = render_report(ROWS)
        for name in {r["model"] for r in ROWS}:
            self.assertIn(name, text)

    def test_model_lines_match_model_report(self):
        text = render_report(ROWS)
        # columns: model, sess, rej%, pass%, err%, avg_s, avg_tok
        self.assertEqual(
            rendered_line(text, MODEL_ALPHA).split(),
            ["alpha-model", "4", "0%", "100%", "25%", "150.0", "9692"])
        self.assertEqual(
            rendered_line(text, MODEL_BETA).split(),
            ["beta-model", "6", "50%", "50%", "17%", "650.0", "6500"])

    def test_stage_lines_match_stage_report(self):
        text = render_report(ROWS)
        # columns: stage, sess, bounce%, avg_s, avg_tok
        self.assertEqual(
            rendered_line(text, "slice_implement").split(),
            ["slice_implement", "5", "20%", "480.0", "4800"])
        self.assertEqual(
            rendered_line(text, "spec_author").split(),
            ["spec_author", "3", "33%", "466.7", "4666"])
        self.assertEqual(
            rendered_line(text, "tech_review").split(),
            ["tech_review", "2", "0%", "350.0", "19884"])

    def test_task_lines_match_task_report(self):
        text = render_report(ROWS)
        line = rendered_line(text, "t1")
        self.assertIn("sessions=3", line)
        self.assertIn("tokens=12000", line)
        self.assertIn("1200.0s", line)
        self.assertIn("bounces=1", line)
        # The task-less group renders under the literal string "None".
        none_line = rendered_line(text, "None")
        self.assertIn("sessions=1", none_line)
        self.assertIn("tokens=7000", none_line)
        self.assertIn("bounces=0", none_line)

    def test_total_count_header_matches_the_fixture(self):
        self.assertIn(f"=== Session stats ({len(ROWS)} total) ===",
                      render_report(ROWS))


class DivisionGuardTest(unittest.TestCase):
    """Degenerate inputs: the report must render, not divide by zero."""

    def test_all_error_model_has_no_decided_sessions(self):
        rows = [row(model="gamma", verdict="error", outcome="error", rc=1,
                    duration_s=10.0, peak_tokens=100),
                row(model="gamma", verdict="error", outcome="error", rc=1,
                    duration_s=20.0, peak_tokens=200)]
        m = model_report(rows)[0]
        self.assertEqual(m["sessions"], 2)
        self.assertEqual(m["rejections"], 0)
        # decided is empty -> the rates are None, not ZeroDivisionError.
        self.assertIsNone(m["rejection_rate"])
        self.assertIsNone(m["pass_rate"])
        self.assertEqual(m["error_rate"], 1.0)          # 2 / 2
        self.assertEqual(m["avg_duration_s"], 15.0)
        self.assertEqual(m["avg_peak_tokens"], 150)
        self.assertIsInstance(render_report(rows), str)

    def test_all_zero_duration_model(self):
        rows = [row(model="delta", duration_s=0.0, peak_tokens=0),
                row(model="delta", duration_s=0.0, peak_tokens=0)]
        m = model_report(rows)[0]
        self.assertEqual(m["avg_duration_s"], 0.0)
        self.assertEqual(m["avg_peak_tokens"], 0)
        # `avg_duration_s or 0` in the renderer must not turn 0.0 into a crash
        # or a missing column.
        self.assertEqual(
            rendered_line(render_report(rows), "delta").split(),
            ["delta", "2", "0%", "100%", "0%", "0.0", "0"])

    def test_single_row_model(self):
        m = model_report([row()])[0]
        self.assertEqual(m["sessions"], 1)
        self.assertEqual(m["pass_rate"], 1.0)
        self.assertEqual(m["rejection_rate"], 0.0)
        self.assertEqual(m["error_rate"], 0.0)

    def test_empty_rows_render_without_error(self):
        self.assertEqual(stage_report([]), [])
        self.assertEqual(task_report([]), [])
        self.assertIsInstance(render_report([]), str)


class StatsStoreTest(unittest.TestCase):
    """`StatsStore` append/read, in a temp dir, never the live store."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t39-stats-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.store_path = self.tmp / "stats" / "sessions.jsonl"
        self.store = StatsStore(self.store_path)

    def test_path_under_test_is_not_the_live_store(self):
        # The live JSONL lives under the operator's work dir, never under a
        # tempdir; asserting containment is what keeps this test from ever
        # appending to real history.
        self.assertTrue(self.store.path.resolve()
                        .is_relative_to(self.tmp.resolve()))
        self.assertNotEqual(self.store.path.resolve(),
                            (Path.home() / "work" / "stats" /
                             "sessions.jsonl").resolve())

    def test_append_then_read_returns_the_same_rows_in_order(self):
        # The card's guard, restated where the writes happen: this store may
        # only ever point inside the tempdir.
        self.assertTrue(self.store.path.resolve()
                        .is_relative_to(self.tmp.resolve()))
        records = [
            SessionRecord(**row(task_id="r1", model=MODEL_ALPHA,
                                peak_tokens=11, duration_s=1.5)),
            SessionRecord(**row(task_id="r2", model=MODEL_BETA,
                                peak_tokens=22, duration_s=2.5,
                                verdict="kickback", outcome="kickback")),
            SessionRecord(**row(task_id="r3", model=MODEL_ALPHA,
                                peak_tokens=33, duration_s=3.5,
                                verdict="error", outcome="error", rc=1)),
        ]
        for rec in records:
            self.store.record(rec)

        rows = self.store.all()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(isinstance(r, dict) for r in rows))
        self.assertEqual([r["task_id"] for r in rows], ["r1", "r2", "r3"])
        self.assertEqual([r["peak_tokens"] for r in rows], [11, 22, 33])
        self.assertEqual([r["duration_s"] for r in rows], [1.5, 2.5, 3.5])
        self.assertEqual(rows, [dataclasses.asdict(r) for r in records])

    def test_record_appends_rather_than_rewrites(self):
        self.store.record(SessionRecord(**row(task_id="first")))
        self.store.record(SessionRecord(**row(task_id="second")))
        self.assertEqual(len(self.store.path.read_text().splitlines()), 2)
        self.store.record(SessionRecord(**row(task_id="third")))
        self.assertEqual([r["task_id"] for r in self.store.all()],
                         ["first", "second", "third"])

    def test_creates_missing_parent_directories(self):
        nested = self.tmp / "deep" / "deeper" / "sessions.jsonl"
        self.assertFalse(nested.parent.exists())
        store = StatsStore(nested)
        self.assertTrue(nested.exists())
        self.assertEqual(store.all(), [])

    def test_blank_and_corrupt_lines_are_skipped(self):
        self.store.record(SessionRecord(**row(task_id="good1")))
        with self.store.path.open("a") as f:
            f.write("\n{not json\n")
        self.store.record(SessionRecord(**row(task_id="good2")))
        self.assertEqual([r["task_id"] for r in self.store.all()],
                         ["good1", "good2"])

    def test_for_task_filters_without_touching_other_rows(self):
        self.store.record(SessionRecord(**row(task_id="a")))
        self.store.record(SessionRecord(**row(task_id="b")))
        self.store.record(SessionRecord(**row(task_id="a")))
        self.assertEqual(len(self.store.for_task("a")), 2)
        self.assertEqual(self.store.for_task("missing"), [])

    def tearDown(self):
        # Belt and braces: nothing this class wrote may leave the tempdir.
        self.assertTrue(self.store.path.resolve()
                        .is_relative_to(self.tmp.resolve()))


if __name__ == "__main__":
    unittest.main()
