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
    model_stage_report,
    render_report,
    render_report_json,
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


MATRIX_HEADER = "--- By Model & Stage (Performance Matrix) ---"


def matrix_data_lines(text: str) -> list[str]:
    """The matrix section's data lines (header and column header excluded)."""
    lines = text.splitlines()
    start = lines.index(MATRIX_HEADER) + 2
    out: list[str] = []
    for ln in lines[start:]:
        if not ln or ln.startswith("---"):
            break
        out.append(ln)
    return out


def strip_matrix_section(text: str) -> str:
    """The report with the matrix section (header + rows) cut out.

    Used to compare the *pre-existing* sections against a snapshot: the blank
    line preceding the matrix header belonged to the matrix section, so it is
    cut with it and the By-task section keeps its own separating blank line.
    """
    lines = text.splitlines()
    start = lines.index(MATRIX_HEADER)
    end = next(i for i in range(start, len(lines))
               if lines[i].startswith("--- By task"))
    return "\n".join(lines[:start] + lines[end:])


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


def by_pair(model: str, stage: str) -> dict:
    matches = [p for p in model_stage_report(ROWS)
               if p["model"] == model and p["stage"] == stage]
    assert len(matches) == 1, f"({model!r}, {stage!r}) missing from model_stage_report"
    return matches[0]


class ModelStageReportTest(unittest.TestCase):
    """`model_stage_report`: per-(model, stage) matrix, hand-computed."""

    def test_pairs_are_sorted_and_complete(self):
        report = model_stage_report(ROWS)
        self.assertEqual(
            [(p["model"], p["stage"]) for p in report],
            [(MODEL_ALPHA, "slice_implement"),
             (MODEL_ALPHA, "spec_author"),
             (MODEL_ALPHA, "tech_review"),
             (MODEL_BETA, "slice_implement"),
             (MODEL_BETA, "spec_author"),
             (MODEL_BETA, "tech_review")])
        # Every pair appears exactly once and the sessions cover the fixture.
        self.assertEqual(sum(p["sessions"] for p in report), len(ROWS))

    def test_alpha_slice_implement_pair(self):
        # A2 done (200.0s, 2000) + A3 error (300.0s, 3000).
        # decided -> A2 only; rejections 0; passes 1; errors 1.
        p = by_pair(MODEL_ALPHA, "slice_implement")
        self.assertEqual(p["sessions"], 2)
        self.assertEqual(p["rejections"], 0)
        self.assertEqual(p["rejection_rate"], 0.0)     # 0 / 1 decided
        self.assertEqual(p["pass_rate"], 1.0)          # 1 / 1 decided
        self.assertEqual(p["error_rate"], 0.5)         # 1 / 2 rows
        self.assertEqual(p["avg_duration_s"], 250.0)   # (200 + 300) / 2
        self.assertEqual(p["avg_peak_tokens"], 2500)   # (2000 + 3000) / 2
        self.assertEqual(p["total_tokens"], 5000)

    def test_beta_slice_implement_pair(self):
        # B2 pass (500.0s, 5000), B3 kickback (600.0s, 6000),
        # B5 error (800.0s, 8000).
        # decided -> B2, B3; rejections -> B3; passes -> B2; errors -> B5.
        p = by_pair(MODEL_BETA, "slice_implement")
        self.assertEqual(p["sessions"], 3)
        self.assertEqual(p["rejections"], 1)
        self.assertEqual(p["rejection_rate"], 0.5)     # 1 / 2 decided
        self.assertEqual(p["pass_rate"], 0.5)          # 1 / 2 decided
        self.assertEqual(p["error_rate"], 0.333)       # 1 / 3, round(.,3)
        # (500 + 600 + 800) / 3 = 633.333 -> round(.,1)
        self.assertEqual(p["avg_duration_s"], 633.3)
        # (5000 + 6000 + 8000) / 3 = 6333.33 -> int 6333
        self.assertEqual(p["avg_peak_tokens"], 6333)
        self.assertEqual(p["total_tokens"], 19000)

    def test_beta_spec_author_pair(self):
        # B1 reject(->unknown, 400.0s, 4000) + B6 fail (900.0s, 9000).
        # decided -> B6 only; rejections -> B6; passes 0; errors 0.
        p = by_pair(MODEL_BETA, "spec_author")
        self.assertEqual(p["sessions"], 2)
        self.assertEqual(p["rejections"], 1)
        self.assertEqual(p["rejection_rate"], 1.0)     # 1 / 1 decided
        self.assertEqual(p["pass_rate"], 0.0)          # 0 / 1 decided
        self.assertEqual(p["error_rate"], 0.0)         # 0 / 2 rows
        self.assertEqual(p["avg_duration_s"], 650.0)   # (400 + 900) / 2
        self.assertEqual(p["avg_peak_tokens"], 6500)   # (4000 + 9000) / 2
        self.assertEqual(p["total_tokens"], 13000)

    def test_all_error_pair_has_no_decided_rows(self):
        rows = [
            row(model="m-err", stage="s1", verdict="error",
                outcome="error", duration_s=10.0, peak_tokens=100),
            row(model="m-err", stage="s1", verdict="error",
                outcome="error", duration_s=30.0, peak_tokens=300),
        ]
        p = model_stage_report(rows)[0]
        self.assertIsNone(p["rejection_rate"])   # no decided rows
        self.assertIsNone(p["pass_rate"])        # no decided rows
        self.assertEqual(p["error_rate"], 1.0)   # 2 / 2
        self.assertEqual(p["avg_duration_s"], 20.0)   # (10 + 30) / 2
        self.assertEqual(p["avg_peak_tokens"], 200)   # (100 + 300) / 2
        self.assertEqual(p["total_tokens"], 400)

    def test_undecided_only_pair(self):
        # All rows outcome `unknown` (the `reject` verdict quirk): nothing is
        # decided, so both decided-rates are None and error_rate is 0.0.
        rows = [
            row(model="m-und", stage="s1", verdict="reject",
                outcome="unknown", duration_s=40.0, peak_tokens=400),
        ]
        p = model_stage_report(rows)[0]
        self.assertIsNone(p["rejection_rate"])
        self.assertIsNone(p["pass_rate"])
        self.assertEqual(p["error_rate"], 0.0)   # 0 errors / 1 row

    def test_single_row_pair_has_exact_rates(self):
        # A4: alpha/tech_review, outcome unknown, 0.0s, at the 32k cap.
        # One row, no division by zero; the cap value formats without blowing up.
        p = by_pair(MODEL_ALPHA, "tech_review")
        self.assertEqual(p["sessions"], 1)
        self.assertIsNone(p["rejection_rate"])   # 0 decided rows
        self.assertIsNone(p["pass_rate"])
        self.assertEqual(p["error_rate"], 0.0)   # 0 / 1
        self.assertEqual(p["avg_duration_s"], 0.0)
        self.assertEqual(p["avg_peak_tokens"], PEAK_TOKENS_CAP)
        self.assertEqual(p["total_tokens"], PEAK_TOKENS_CAP)

    def test_empty_rows_produce_no_report_rows(self):
        self.assertEqual(model_stage_report([]), [])

    def test_missing_model_and_stage_keys_group_as_unknown(self):
        r = row()
        del r["model"]
        del r["stage"]
        report = model_stage_report([r])
        self.assertEqual([(p["model"], p["stage"]) for p in report],
                         [("unknown", "unknown")])

    def test_none_values_group_as_none_string(self):
        # Pinned `_group` behaviour: str(None) == "None", not "unknown".
        rows = [row(model=None, stage=None)]
        report = model_stage_report(rows)
        self.assertEqual([(p["model"], p["stage"]) for p in report],
                         [("None", "None")])


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
        # The matrix rows also start with the model name, so the By-model
        # lines are located in the report with the matrix section removed.
        text = strip_matrix_section(render_report(ROWS))
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

    def test_matrix_section_header_present(self):
        self.assertIn(MATRIX_HEADER, render_report(ROWS))

    def test_matrix_section_sits_between_stage_and_task(self):
        lines = render_report(ROWS).splitlines()
        stage_i = lines.index("--- By stage (bounce rate) ---")
        matrix_i = lines.index(MATRIX_HEADER)
        task_i = lines.index("--- By task ---")
        self.assertLess(stage_i, matrix_i)
        self.assertLess(matrix_i, task_i)
        # Leading blank line, like the other sections.
        self.assertEqual(lines[matrix_i - 1], "")

    def test_matrix_column_header_lists_every_column(self):
        lines = render_report(ROWS).splitlines()
        header = lines[lines.index(MATRIX_HEADER) + 1]
        self.assertEqual(
            header.split(),
            ["model", "stage", "sess", "rej%", "pass%", "err%",
             "avg_s", "avg_tok"])

    def test_matrix_lines_match_model_stage_report(self):
        # One data line per `model_stage_report` entry, in the same order,
        # each carrying model, stage, sessions, rej/pass/err, avg_s, avg_tok.
        text = render_report(ROWS)
        report = model_stage_report(ROWS)
        data = matrix_data_lines(text)
        self.assertEqual(len(data), len(report))
        # Hand-computed from the fixture (see ModelStageReportTest), in
        # (model, stage) sort order — `slice_implement` precedes `spec_author`:
        # alpha/slice_implement: A2 done + A3 error -> 0/1, 1/1, 1/2.
        self.assertEqual(
            data[0].split(),
            ["alpha-model", "slice_implement", "2", "0%", "100%", "50%",
             "250.0", "2500"])
        # alpha/spec_author: A1 -> 1 sess, 0/1 rej, 1/1 pass, 0/1 err.
        self.assertEqual(
            data[1].split(),
            ["alpha-model", "spec_author", "1", "0%", "100%", "0%",
             "100.0", "1000"])
        # alpha/tech_review: A4 undecided-only -> both decided-rates None,
        # which `_pct` renders as "  -"; the 32k-cap token value formats.
        self.assertEqual(
            data[2].split(),
            ["alpha-model", "tech_review", "1", "-", "-", "0%",
             "0.0", "32768"])
        # beta/slice_implement: B2, B3, B5 -> 1/2, 1/2, 1/3 (rounds to 33%).
        self.assertEqual(
            data[3].split(),
            ["beta-model", "slice_implement", "3", "50%", "50%", "33%",
             "633.3", "6333"])
        # beta/spec_author: B1 unknown + B6 fail -> 1/1, 0/1, 0/2.
        self.assertEqual(
            data[4].split(),
            ["beta-model", "spec_author", "2", "100%", "0%", "0%",
             "650.0", "6500"])
        # beta/tech_review: B4 done only -> 0/1, 1/1, 0/1.
        self.assertEqual(
            data[5].split(),
            ["beta-model", "tech_review", "1", "0%", "100%", "0%",
             "700.0", "7000"])

    def test_empty_rows_still_print_matrix_headers(self):
        lines = render_report([]).splitlines()
        self.assertIn(MATRIX_HEADER, lines)
        header = lines[lines.index(MATRIX_HEADER) + 1]
        self.assertEqual(
            header.split(),
            ["model", "stage", "sess", "rej%", "pass%", "err%",
             "avg_s", "avg_tok"])
        self.assertEqual(matrix_data_lines(render_report([])), [])

    def test_existing_sections_are_byte_for_byte_unchanged(self):
        # AC-3: the matrix is additive. With the matrix section cut out, the
        # rest of the report must equal the pre-auto-30 snapshot exactly.
        self.assertEqual(strip_matrix_section(render_report(ROWS)),
                         LEGACY_REPORT)


# `render_report(ROWS)` before auto-30 added the matrix section, captured
# verbatim; `test_existing_sections_are_byte_for_byte_unchanged` diffs against
# it so a change to an existing column shows up here, not in a review comment.
LEGACY_REPORT = """\
=== Session stats (10 total) ===

--- By model (quality & speed) ---
model                                                 sess   rej%  pass%   err%   avg_s  avg_tok
alpha-model                                              4     0%   100%    25%   150.0     9692
beta-model                                               6    50%    50%    17%   650.0     6500

--- By stage (bounce rate) ---
stage                   sess  bounce%   avg_s  avg_tok
slice_implement            5      20%   480.0     4800
spec_author                3      33%   466.7     4666
tech_review                2       0%   350.0    19884

--- By task ---
None                                     sessions=1    tokens=7000      time=   700.0s bounces=0
t1                                       sessions=3    tokens=12000     time=  1200.0s bounces=1
t2                                       sessions=3    tokens=39768     time=   700.0s bounces=0
t3                                       sessions=3    tokens=19000     time=  1900.0s bounces=1"""


class MatrixReconciliationTest(unittest.TestCase):
    """The matrix must reconcile with `model_report`/`stage_report` (AC-4).

    Same outcome definitions on every level: a pair's numbers are the
    `model_report` formulas over the pair's rows, so summing the pairs along
    either axis reproduces the parent tables' counts exactly.
    """

    def test_pair_sessions_sum_to_model_report_sessions(self):
        report = model_stage_report(ROWS)
        for m in model_report(ROWS):
            pairs = [p for p in report if p["model"] == m["model"]]
            self.assertEqual(sum(p["sessions"] for p in pairs),
                             m["sessions"], m["model"])

    def test_pair_sessions_sum_to_stage_report_sessions(self):
        report = model_stage_report(ROWS)
        for s in stage_report(ROWS):
            pairs = [p for p in report if p["stage"] == s["stage"]]
            self.assertEqual(sum(p["sessions"] for p in pairs),
                             s["sessions"], s["stage"])

    def test_pair_rejections_sum_to_model_report_rejections(self):
        report = model_stage_report(ROWS)
        for m in model_report(ROWS):
            pairs = [p for p in report if p["model"] == m["model"]]
            self.assertEqual(sum(p["rejections"] for p in pairs),
                             m["rejections"], m["model"])

    def test_pair_rejections_sum_to_stage_report_bounces(self):
        # `stage_report` counts bounces over all rows, `model_stage_report`
        # over decided rows — equal because kickback/fail/kickout are always
        # decided. This pins that the two outcome lists stay in step.
        report = model_stage_report(ROWS)
        for s in stage_report(ROWS):
            pairs = [p for p in report if p["stage"] == s["stage"]]
            self.assertEqual(sum(p["rejections"] for p in pairs),
                             s["bounces"], s["stage"])

    def test_pair_total_tokens_sum_to_model_report_totals(self):
        report = model_stage_report(ROWS)
        for m in model_report(ROWS):
            pairs = [p for p in report if p["model"] == m["model"]]
            self.assertEqual(sum(p["total_tokens"] for p in pairs),
                             m["total_tokens"], m["model"])

    def test_beta_slice_implement_pair_agrees_with_both_parents(self):
        # The discriminating pair: it feeds model beta's rejection/pass
        # rates and stage slice_implement's bounce rate simultaneously.
        p = by_pair(MODEL_BETA, "slice_implement")
        m = by_model(MODEL_BETA)
        s = by_stage("slice_implement")
        # pair: B2 pass, B3 kickback, B5 error -> 1/2, 1/2, 1/3.
        self.assertEqual((p["rejection_rate"], p["pass_rate"],
                          p["error_rate"]), (0.5, 0.5, 0.333))
        # model beta's 2 rejections = B3 (this pair) + B6 (spec_author pair).
        self.assertEqual(m["rejections"], 2)
        self.assertEqual(p["rejections"]
                         + by_pair(MODEL_BETA, "spec_author")["rejections"],
                         m["rejections"])
        # stage slice_implement's single bounce is B3, this pair's rejection.
        self.assertEqual(s["bounces"], p["rejections"])

    def test_single_pair_model_and_stage_share_the_pair_rates(self):
        # A model with exactly one stage and a stage with exactly one model:
        # all three levels must report the same rates for the same rows.
        rows = [
            row(model="solo", stage="solo_stage", verdict="pass",
                outcome="pass"),
            row(model="solo", stage="solo_stage", verdict="kickback",
                outcome="kickback"),
        ]
        p = model_stage_report(rows)[0]
        m = model_report(rows)[0]
        s = stage_report(rows)[0]
        self.assertEqual(p["rejection_rate"], 0.5)      # 1 / 2 decided
        self.assertEqual(p["rejection_rate"], m["rejection_rate"])
        self.assertEqual(p["pass_rate"], m["pass_rate"])
        self.assertEqual(p["error_rate"], m["error_rate"])
        self.assertEqual(s["bounce_rate"], p["rejection_rate"])  # 1 / 2 rows


class ReportJsonTest(unittest.TestCase):
    """`harness.py report-json` exposes the matrix under `model_stages` (G3)."""

    def test_model_stages_key_equals_model_stage_report(self):
        self.assertEqual(render_report_json(ROWS)["model_stages"],
                         model_stage_report(ROWS))

    def test_model_stages_is_json_serialisable(self):
        # The whole payload must survive a JSON round trip — the pair keys
        # are plain str/float/int/None, no dataclasses leak in.
        import json
        payload = json.loads(json.dumps(render_report_json(ROWS)))
        self.assertEqual(payload["model_stages"], model_stage_report(ROWS))

    def test_pre_existing_keys_are_unchanged(self):
        # AC-3: `model_stages` is additive; every prior key keeps its value.
        result = render_report_json(ROWS)
        self.assertEqual(result["total_sessions"], 10)
        self.assertEqual(result["total_tokens"],
                         sum(r["peak_tokens"] for r in ROWS))  # 77768
        self.assertEqual(result["models"], model_report(ROWS))
        self.assertEqual(result["stages"], stage_report(ROWS))
        self.assertEqual(result["tasks"], task_report(ROWS))

    def test_empty_rows_produce_empty_model_stages(self):
        result = render_report_json([])
        self.assertEqual(result["model_stages"], [])
        self.assertEqual(result["total_sessions"], 0)
        self.assertEqual(result["total_tokens"], 0)


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
            rendered_line(strip_matrix_section(render_report(rows)),
                          "delta").split(),
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
