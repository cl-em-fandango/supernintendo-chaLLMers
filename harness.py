#!/usr/bin/env python3
"""CLI entry point for the autonomous workflow harness.

Usage — subcommands and flags exactly as cli/parser.py defines them:
  harness.py run                 Process all pending tasks, then autonomous mode
                                 (--continue, --requeue-stale)
  harness.py run-task <file>     Process one task file (--continue, --fresh)
  harness.py run-one             Claim and process exactly one pending task
  harness.py run-task-loop       Process pending tasks until the queue is empty
                                 (--continue, --requeue-stale)
  harness.py autonomous          Generate tasks until queue has N
  harness.py status              Show queue + stats
  harness.py report              Print the stats report
  harness.py board               Kanban-style queue view with executive summary
                                 (hidden alias: kanban)
  harness.py journey [task_id]   Show workflow journey graph & bottleneck analysis
  harness.py resume <task_id>    Resume a task from its last checkpoint
                                 (--yes / -y, --fresh)
  harness.py unpark <task_id>    Resume a parked/failed task (synonym for resume)
                                 (--yes / -y, --fresh; hidden alias: requeue)
  harness.py restart <task_id>   Restart a task from scratch, dropping checkpoints
                                 (--yes / -y)
  harness.py requeue-claims      Hand stranded claimed/ files back to pending
                                 (--older-than HOURS, --dry-run)

--continue (run, run-task, run-task-loop) resumes in-flight tasks in active/
before the pending queue is worked. --fresh (run-task) deletes any existing
active/ dir and restarts that task from scratch. --yes skips resume's
confirmation prompt. --requeue-stale (run, run-task-loop) reclaims claims older
than CLAIM_STALE_HOURS at startup (off unless flagged, or
"autoRequeueStaleClaims": true in config.json).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.cli.parser import parse_args
from harness.cli import handlers
from harness.core.environment import assert_containerized


def main() -> int:
    assert_containerized("harness.py")
    args = parse_args(sys.argv[1:])
    
    if args.command is None:
        print(__doc__)
        return 1
    
    if args.command == "run":
        return handlers.cmd_run(continue_=args.continue_,
                                requeue_stale=args.requeue_stale)
    elif args.command == "run-task":
        return handlers.cmd_run_task(args.file, fresh=args.fresh,
                                     continue_=args.continue_)
    elif args.command == "run-one":
        return handlers.cmd_run_one()
    elif args.command == "run-task-loop":
        return handlers.cmd_run_task_loop(continue_=args.continue_,
                                           requeue_stale=args.requeue_stale)
    elif args.command == "autonomous":
        return handlers.cmd_autonomous()
    elif args.command == "status":
        return handlers.cmd_status()
    elif args.command == "report":
        return handlers.cmd_report()
    elif args.command in ("board", "kanban"):
        return handlers.cmd_board()
    elif args.command == "journey":
        return handlers.cmd_journey(task_id=args.task_id, save=args.save)
    elif args.command == "resume":
        return handlers.cmd_resume(args.task_id, args.yes, fresh=args.fresh)
    elif args.command in ("unpark", "requeue"):
        return handlers.cmd_unpark(args.task_id, yes=args.yes, fresh=args.fresh)
    elif args.command == "restart":
        return handlers.cmd_restart(args.task_id, yes=args.yes)
    elif args.command == "requeue-claims":
        return handlers.cmd_requeue_claims(older_than=args.older_than,
                                           dry_run=args.dry_run)
    
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())