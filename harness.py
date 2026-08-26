#!/usr/bin/env python3
"""CLI entry point for the autonomous workflow harness.

Usage:
  harness.py run                 Process all pending tasks, then autonomous mode
  harness.py run-task <file>     Process a single task file (--fresh, --continue)
  harness.py run-task-loop       Process pending tasks until queue is empty (--continue)
  harness.py resume <task_id>    Resume a task from its last checkpoint
  harness.py autonomous          Generate tasks until queue has N
  harness.py status              Show queue + stats
  harness.py report              Print the stats report
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.composition import build
from harness.cli.parser import parse_args
from harness.cli import handlers

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def main() -> int:
    args = parse_args(sys.argv[1:])
    
    if args.command is None:
        print(__doc__)
        return 1
    
    if args.command == "run":
        return handlers.cmd_run(continue_=args.continue_)
    elif args.command == "run-task":
        return handlers.cmd_run_task(args.file, fresh=args.fresh,
                                     continue_=args.continue_)
    elif args.command == "run-one":
        return handlers.cmd_run_one()
    elif args.command == "run-task-loop":
        return handlers.cmd_run_task_loop(continue_=args.continue_)
    elif args.command == "autonomous":
        return handlers.cmd_autonomous()
    elif args.command == "status":
        return handlers.cmd_status()
    elif args.command == "report":
        return handlers.cmd_report()
    elif args.command == "resume":
        return handlers.cmd_resume(args.task_id, args.yes)
    elif args.command in ("unpark", "requeue"):
        return handlers.cmd_unpark(args.task_id)
    
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())