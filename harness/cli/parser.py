"""Argument parser for the harness CLI."""
from __future__ import annotations

import argparse
from typing import List, Optional


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

    # unpark (with requeue alias)
    unpark_parser = subparsers.add_parser("unpark", help="Move a parked/failed task back to pending")
    unpark_parser.add_argument("task_id", help="Task ID to unpark")
    
    # Add requeue as an alias
    requeue_parser = subparsers.add_parser("requeue", help=argparse.SUPPRESS)
    requeue_parser.add_argument("task_id", help=argparse.SUPPRESS)
    
    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = build_parser()
    return parser.parse_args(argv)