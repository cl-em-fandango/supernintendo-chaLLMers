"""Argument parser for the harness CLI."""
from __future__ import annotations

import argparse
from typing import List, Optional


REQUEUE_STALE_HELP = (
    "At startup, move claims older than CLAIM_STALE_HOURS (default 6h) back to "
    "pending/. OFF by default: what is in claimed/ now is the input to the "
    "human review pass, and an always-on guard would empty the dir before "
    "anyone read it. T13 may treat claimed/ as work only once an operator has "
    "turned this on. Also settable as \"autoRequeueStaleClaims\": true in config.json.")


def _add_requeue_stale_flag(parser: argparse.ArgumentParser) -> None:
    """The opt-in loop-start stale-claim guard, shared by both long-running commands."""
    parser.add_argument("--requeue-stale", dest="requeue_stale", action="store_true",
                        default=False, help=REQUEUE_STALE_HELP)


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
    
    # run-task
    run_task_parser = subparsers.add_parser("run-task", help="Process a single task file")
    run_task_parser.add_argument("file", help="Path to the task file")
    run_task_parser.add_argument("--continue", dest="continue_", action="store_true",
                                default=False, help="Also resume in-flight tasks in active/")
    run_task_parser.add_argument("--fresh", dest="fresh", action="store_true",
                                default=False, help="Delete any existing active/ dir and restart from scratch")
    
    # run-one
    subparsers.add_parser("run-one", help="Claim and process exactly one pending task")
    
    # run-task-loop
    run_task_loop_parser = subparsers.add_parser("run-task-loop", help="Process pending tasks one at a time until queue is empty")
    run_task_loop_parser.add_argument("--continue", dest="continue_", action="store_true",
                                     default=False, help="Also resume in-flight tasks in active/")
    _add_requeue_stale_flag(run_task_loop_parser)
    
    # autonomous
    subparsers.add_parser("autonomous", help="Generate tasks until queue has N")
    
    # status
    subparsers.add_parser("status", help="Show queue + stats")
    
    # report
    subparsers.add_parser("report", help="Print the stats report")
    
    # resume
    resume_parser = subparsers.add_parser("resume", help="Resume a task from its last checkpoint")
    resume_parser.add_argument("task_id", help="Task ID to resume")
    resume_parser.add_argument("--yes", "-y", dest="yes", action="store_true",
                              default=False, help="Skip the confirmation prompt")
    resume_parser.add_argument("--fresh", dest="fresh", action="store_true",
                               default=False,
                               help="Drop all checkpoints and restart from scratch")

    # unpark (with requeue alias)
    unpark_parser = subparsers.add_parser("unpark", help="Move a parked/failed task back to pending")
    unpark_parser.add_argument("task_id", help="Task ID to unpark")
    
    # Add requeue as an alias
    requeue_parser = subparsers.add_parser("requeue", help=argparse.SUPPRESS)
    requeue_parser.add_argument("task_id", help=argparse.SUPPRESS)

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