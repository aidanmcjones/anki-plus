"""ankifix command line."""
from __future__ import annotations

import argparse
import shlex
import sys
from typing import List, Optional

from . import agent as agent_mod
from . import apply as apply_mod
from .config import load_config
from .runner import AnkifixError, LockBusy, plan, run_ticket, ticket_lock, watch
from .tickets import load_ticket, write_index

SUBCOMMANDS = ("apply", "install-agent", "uninstall-agent", "doctor")


def build_apply_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ankifix apply",
        description="Apply an app ticket's fix branch to the live add-on checkout "
        "(git apply --check then git apply; no commit).",
    )
    ap.add_argument("ticket_id")
    ap.add_argument("--config", help="config JSON (default ~/.config/ankifix/config.json)")
    return ap


def build_install_agent_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ankifix install-agent",
        description="Write and load the com.aidanjones.ankifix-watch LaunchAgent "
        "(RunAtLoad + KeepAlive). Refuses if a non-launchd `ankifix --watch` is running.",
    )
    ap.add_argument("--interval", type=float, default=20, help="--watch poll interval in seconds (default 20)")
    ap.add_argument("--config", help="config JSON (default ~/.config/ankifix/config.json)")
    return ap


def build_uninstall_agent_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ankifix uninstall-agent",
        description="Unload and remove the com.aidanjones.ankifix-watch LaunchAgent.",
    )
    ap.add_argument("--config", help="config JSON (default ~/.config/ankifix/config.json)")
    return ap


def cmd_apply(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    try:
        apply_mod.apply_ticket(args.ticket_id, cfg)
        return 0
    except (AnkifixError, FileNotFoundError) as e:
        print(f"ankifix: {e}", file=sys.stderr)
        return 2


def build_doctor_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ankifix doctor",
        description="Check that the watcher can do its job: tools on the agent PATH, Playwright "
        "NODE_PATH, addon repo, tickets dir, course build dir (macOS Files access), launchd "
        "agent, notifications. Exits 1 on any FAIL.",
    )
    ap.add_argument("--config", help="config JSON (default ~/.config/ankifix/config.json)")
    return ap


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor as doctor_mod

    return doctor_mod.doctor(load_config(args.config))


def cmd_install_agent(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    try:
        agent_mod.install_agent(interval=args.interval, cfg=cfg)
        return 0
    except AnkifixError as e:
        print(f"ankifix: {e}", file=sys.stderr)
        return 2


def cmd_uninstall_agent(args: argparse.Namespace) -> int:
    load_config(args.config)
    agent_mod.uninstall_agent()
    return 0


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
    ap.add_argument(
        "--notify", dest="notify", action="store_true", default=None,
        help="with --watch: post a macOS notification after each ticket (default: on; see the "
        "notify config key)",
    )
    ap.add_argument(
        "--no-notify", dest="notify", action="store_false",
        help="with --watch: disable the notification set by the notify config key",
    )
    ap.add_argument(
        "--once-from-terminal", action="store_true",
        help="internal: the Terminal.app side of the watcher's Files-access fallback; runs this "
        "one ticket without taking the lock (the waiting watcher holds it)",
    )
    ap.add_argument("--config", help="config JSON (default ~/.config/ankifix/config.json)")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Manual dispatch (like ankibug's cli): only an exact, bare argv[0] is a
    # subcommand, since the default form's ticket_id positional could in
    # principle collide - it never does in practice (ticket ids start with
    # a timestamp) but this keeps the same discipline as ankibug/cli.py.
    if argv and argv[0] in SUBCOMMANDS:
        name, rest = argv[0], argv[1:]
        if name == "apply":
            return cmd_apply(build_apply_parser().parse_args(rest))
        if name == "install-agent":
            return cmd_install_agent(build_install_agent_parser().parse_args(rest))
        if name == "uninstall-agent":
            return cmd_uninstall_agent(build_uninstall_agent_parser().parse_args(rest))
        if name == "doctor":
            return cmd_doctor(build_doctor_parser().parse_args(rest))

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
    if args.notify is not None:
        cfg.notify = args.notify

    if args.reindex:
        print(write_index(cfg.tickets_dir))
        return 0
    if args.watch:
        # under launchd stdout is a file, so Python block-buffers it and the
        # log shows nothing until 8 KB accumulate; line-buffer instead
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
            except (AttributeError, ValueError):
                pass
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
        if args.once_from_terminal:
            t = run_ticket(args.ticket_id, cfg, kind_override=args.kind, force=True,
                           from_terminal=True)
        else:
            with ticket_lock(cfg.tickets_dir):
                t = run_ticket(args.ticket_id, cfg, kind_override=args.kind, force=args.force)
        return 0 if t.get("status") == "fixed" else 1
    except (AnkifixError, FileNotFoundError) as e:
        print(f"ankifix: {e}", file=sys.stderr)
        return 3 if isinstance(e, LockBusy) else 2


if __name__ == "__main__":
    raise SystemExit(main())
