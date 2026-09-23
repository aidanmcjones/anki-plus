"""ankifix command line."""
from __future__ import annotations

import argparse
import shlex
import sys
from typing import List, Optional

from .config import load_config
from .runner import AnkifixError, LockBusy, plan, run_ticket, ticket_lock, watch
from .tickets import load_ticket, write_index


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ankifix",
        description="Turn an AnkiTickets ticket into a fix (app: git branch; deck: rebuilt deck) "
        "by running `claude -p` headless.",
    )
    ap.add_argument("ticket_id", nargs="?", help="ticket id (directory name under the tickets dir)")
    ap.add_argument("--watch", action="store_true", help="loop over status=new tickets, one at a time")
    ap.add_argument("--interval", type=float, default=30, help="--watch poll interval in seconds (default 30)")
    ap.add_argument("--once", action="store_true", help="with --watch: drain the queue once and exit")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt (and command on stderr); do nothing")
    ap.add_argument("--kind", choices=["app", "deck"], help="override classification")
    ap.add_argument("--max-turns", type=int, help="claude --max-turns (default 60)")
    ap.add_argument("--timeout-min", type=float, help="per-ticket wall clock limit in minutes (default 25)")
    ap.add_argument("--max-budget-usd", type=float, help="claude --max-budget-usd (default: none)")
    ap.add_argument("--model", help="claude --model (default: claude's default)")
    ap.add_argument("--force", action="store_true", help="run even if the ticket is fixed/fixing/wontfix")
    ap.add_argument("--reindex", action="store_true", help="only regenerate index.md")
    ap.add_argument("--config", help="config JSON (default ~/.config/ankifix/config.json)")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.max_turns:
        cfg.max_turns = args.max_turns
    if args.timeout_min:
        cfg.wall_clock_minutes = args.timeout_min
    if args.max_budget_usd:
        cfg.max_budget_usd = args.max_budget_usd
    if args.model:
        cfg.model = args.model

    if args.reindex:
        print(write_index(cfg.tickets_dir))
        return 0
    if args.watch:
        try:
            watch(cfg, args.interval, once=args.once)
        except KeyboardInterrupt:
            return 130
        return 0
    if not args.ticket_id:
        build_parser().print_usage(sys.stderr)
        return 2
    try:
        if args.dry_run:
            ticket = load_ticket(cfg.tickets_dir, args.ticket_id)
            p = plan(ticket, cfg, args.kind)
            print(f"# kind={p['kind']} ({p['reason']})", file=sys.stderr)
            print(f"# cwd: {p['cwd']}", file=sys.stderr)
            print(f"# cmd: {shlex.join(p['cmd'])} < prompt", file=sys.stderr)
            print(p["prompt"])
            return 0
        with ticket_lock(cfg.tickets_dir):
            t = run_ticket(args.ticket_id, cfg, kind_override=args.kind, force=args.force)
        return 0 if t.get("status") == "fixed" else 1
    except (AnkifixError, FileNotFoundError) as e:
        print(f"ankifix: {e}", file=sys.stderr)
        return 3 if isinstance(e, LockBusy) else 2


if __name__ == "__main__":
    raise SystemExit(main())
