"""Argument parser for the harness CLI."""
from __future__ import annotations

import argparse
from typing import List, Optional


REQUEUE_STALE_HELP = (
    "At startup, move claims older than CLAIM_STALE_HOURS (default 6h) back to "
    "pending/. OFF by default: what is in claimed/ now is the input to the "
    "human review pass, and an always-on guard would empty the dir before "
    "anyone read it. T13 may treat claimed/ as work only once an operator has "
    "turned this on. Also settable as \"autoRequeueStaleClaims\": true in "
    "config.json. Stays strictly opt-in around interrupts: a managed "
    "stand-down (harness.py interrupt) never reclaims claims by itself, no "
    "matter how long the harness stayed paused — tasks interrupted mid-run "
    "keep their claim in active/ until the operator explicitly requeues "
    "them (this flag, or `requeue-claims`).")


def _add_requeue_stale_flag(parser: argparse.ArgumentParser) -> None:
    """The opt-in loop-start stale-claim guard, shared by both long-running commands."""
    parser.add_argument("--requeue-stale", dest="requeue_stale", action="store_true",
                        default=False, help=REQUEUE_STALE_HELP)


def _add_repo_flag(parser: argparse.ArgumentParser) -> None:
    """The target repository flag: CLI override for targetCodebaseDir in config.json."""
    parser.add_argument("--repo", "--repo-dir", dest="repo", default=None,
                        help="Path to target git repository (overrides targetCodebaseDir in config.json)")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the harness CLI."""
    parser = argparse.ArgumentParser(
        prog="harness.py",
        description="CLI entry point for the autonomous workflow harness."
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # run
    run_parser = subparsers.add_parser("run", help="Process all pending tasks, then autonomous mode")
    run_parser.add_argument("--continue", dest="continue_", action="store_true",
                           default=False, help="Also resume in-flight tasks in active/")
    _add_requeue_stale_flag(run_parser)
    _add_repo_flag(run_parser)
    
    # run-task
    run_task_parser = subparsers.add_parser("run-task", help="Process a single task file")
    run_task_parser.add_argument("file", help="Path to the task file")
    run_task_parser.add_argument("--continue", dest="continue_", action="store_true",
                                default=False, help="Also resume in-flight tasks in active/")
    run_task_parser.add_argument("--fresh", dest="fresh", action="store_true",
                                default=False, help="Delete any existing active/ dir and restart from scratch")
    _add_repo_flag(run_task_parser)
    
    # run-one
    run_one_parser = subparsers.add_parser("run-one", help="Claim and process exactly one pending task")
    _add_repo_flag(run_one_parser)
    
    # run-task-loop
    run_task_loop_parser = subparsers.add_parser("run-task-loop", help="Process pending tasks one at a time until queue is empty")
    run_task_loop_parser.add_argument("--continue", dest="continue_", action="store_true",
                                     default=False, help="Also resume in-flight tasks in active/")
    _add_requeue_stale_flag(run_task_loop_parser)
    _add_repo_flag(run_task_loop_parser)
    
    # autonomous
    autonomous_parser = subparsers.add_parser("autonomous", help="Generate tasks until queue has N")
    _add_repo_flag(autonomous_parser)
    
    # status
    subparsers.add_parser("status", help="Show queue + stats")
    
    # report
    subparsers.add_parser("report", help="Print the stats report")
    # report-json
    subparsers.add_parser("report-json", help="Print the stats report as JSON")
    # export-stats-csv
    export_csv_parser = subparsers.add_parser("export-stats-csv", help="Export raw session stats to CSV")
    export_csv_parser.add_argument("output", nargs="?", default="stats.csv",
                                 help="Path to output CSV file (default: stats.csv)")
    # stats-prune
    prune_parser = subparsers.add_parser("stats-prune", help="Trim the stats store to recent rows")
    prune_parser.add_argument("--max-rows", dest="max_rows", type=int, default=None,
                                help="Maximum number of recent rows to keep (default from config or 10000)")

    # sync
    subparsers.add_parser(
        "sync", help="Run one two-way GitHub issue sync pass (prints "
                     "\"github sync disabled\" and exits 0 when githubPat/"
                     "githubRepo are unconfigured)")

    # syncd
    subparsers.add_parser(
        "syncd", help="Run the sync daemon: poll, sync, and spawn one "
                      "harness run when pending/ has work and none is "
                      "running (single instance via <harnessExecutionAndQueueDir>/syncd.lock; "
                      "exits non-zero when another syncd holds the lock)")

    # board (with hidden kanban alias)
    board_parser = subparsers.add_parser("board", help="Kanban-style queue view with executive summary")
    board_parser.add_argument("--json", dest="json", action="store_true", default=False,
                            help="Output board data as JSON instead of the formatted view")
    kanban_parser = subparsers.add_parser("kanban", help=argparse.SUPPRESS)
    kanban_parser.add_argument("--json", dest="json", action="store_true", default=False,
                               help="Output board data as JSON instead of the formatted view")

    # journey
    journey_parser = subparsers.add_parser("journey", help="Show static workflow journey graph and bottleneck analysis")
    journey_parser.add_argument("task_id", nargs="?", default=None, help="Task ID (defaults to most recent task)")
    journey_parser.add_argument("--save", dest="save", action="store_true", default=False,
                                help="Save journey graph to <statsDir>/journeys/<task_id>-journey.txt")

    # journey-markdown
    journey_md_parser = subparsers.add_parser("journey-md", help="Export workflow journey as Markdown with transcript links")
    journey_md_parser.add_argument("task_id", nargs="?", default=None, help="Task ID (defaults to most recent task)")
    journey_md_parser.add_argument("--save", dest="save", action="store_true", default=False,
                                help="Save Markdown journey to <statsDir>/journeys/<task_id>-journey.md")
    
    # interrupt
    interrupt_parser = subparsers.add_parser(
        "interrupt", help="Request a managed stand-down of the harness so the "
                          "model is released to the operator",
        description=(
            "With --stand-down: the harness stops taking work at its next "
            "session boundary and stays down until `harness.py resume`. "
            "Without it (quick mode): the harness pauses, one `pi` session "
            "runs against the chosen model with your terminal (interactive; "
            "or one-shot with --prompt), and the harness resumes "
            "automatically when that session exits. Run it through "
            "`scripts/harness-run` so the session gets the same container "
            "context (TTY, model endpoints) as the harness. If this command "
            "is killed before cleanup, the request file remains and the "
            "harness stays paused (fail-safe) — recover with "
            "`harness.py resume`."))
    interrupt_parser.add_argument(
        "--stand-down", dest="stand_down", action="store_true", default=False,
        help="Full stand-down: the harness stops taking work at its next "
             "session boundary and stays down until `harness.py resume`. "
             "Tasks stay in active/ at their checkpoints. If this process is "
             "killed before cleanup, the request file remains — recover with "
             "`harness.py resume`.")
    interrupt_parser.add_argument(
        "--no-wait", dest="no_wait", action="store_true", default=False,
        help="Exit immediately after writing the request instead of waiting "
             "for the harness to pause")
    interrupt_parser.add_argument(
        "--timeout", dest="timeout", type=float, default=None, metavar="SECONDS",
        help="Seconds to wait for the harness to pause "
             "(default: sessionTimeout + 60)")
    interrupt_parser.add_argument(
        "--model", dest="model", default=None, metavar="NAME",
        help="Quick mode: model for the borrowed session; must be a "
             "configured model (a models.* value or a modelContext key) — "
             "pool names (fastPool, randomPool) are rejected (default: "
             "models.technicalWriter from config)")
    interrupt_parser.add_argument(
        "--prompt", dest="prompt", default=None, metavar="TEXT",
        help="Quick mode: run one-shot with this prompt instead of an "
             "interactive TTY session")

    # resume
    resume_parser = subparsers.add_parser(
        "resume", help="Resume a task from its last checkpoint, or (with no "
                       "task_id) clear an active interrupt and resume the run")
    resume_parser.add_argument(
        "task_id", nargs="?", default=None,
        help="Task ID to resume; omit to clear an active interrupt. Neither "
             "form ever reclaims stale claims implicitly: after a stand-down "
             "your own claims stay in active/ with their claim metadata, and "
             "age-based reclaim stays an explicit operator action "
             "(--requeue-stale on run/run-task-loop, or `requeue-claims`)")
    resume_parser.add_argument("--yes", "-y", dest="yes", action="store_true",
                              default=False, help="Skip the confirmation prompt")
    resume_parser.add_argument("--fresh", dest="fresh", action="store_true",
                               default=False,
                               help="Drop all checkpoints and restart from scratch")
    _add_repo_flag(resume_parser)

    # unpark (with requeue alias)
    unpark_parser = subparsers.add_parser("unpark", help="Resume a parked/failed task (synonym for resume)")
    unpark_parser.add_argument("task_id", help="Task ID to unpark / resume")
    unpark_parser.add_argument("--yes", "-y", dest="yes", action="store_true",
                               default=False, help="Skip the confirmation prompt")
    unpark_parser.add_argument("--fresh", dest="fresh", action="store_true",
                               default=False,
                               help="Drop all checkpoints and restart from scratch")
    
    # Add requeue as an alias
    requeue_parser = subparsers.add_parser("requeue", help=argparse.SUPPRESS)
    requeue_parser.add_argument("task_id", help=argparse.SUPPRESS)
    requeue_parser.add_argument("--yes", "-y", dest="yes", action="store_true",
                                default=False, help=argparse.SUPPRESS)
    requeue_parser.add_argument("--fresh", dest="fresh", action="store_true",
                                default=False, help=argparse.SUPPRESS)

    # restart
    restart_parser = subparsers.add_parser("restart", help="Restart a task from scratch (deletes checkpoints)")
    restart_parser.add_argument("task_id", help="Task ID to restart")
    restart_parser.add_argument("--yes", "-y", dest="yes", action="store_true",
                                default=False, help="Skip the confirmation prompt")

    # requeue-claims (operator command: recover stranded claims from claimed/)
    requeue_claims_parser = subparsers.add_parser(
        "requeue-claims", help="Move claimed-but-unprocessed tasks back to pending")
    requeue_claims_parser.add_argument("--older-than", dest="older_than", type=float,
                                       default=0.0, metavar="HOURS",
                                       help="Only requeue claims at least this many hours "
                                            "old (default 0.0 = every claim)")
    requeue_claims_parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                                       default=False,
                                       help="Print what would move; change nothing")
    
    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = build_parser()
    return parser.parse_args(argv)