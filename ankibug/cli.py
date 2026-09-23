"""ankibug CLI.

Usage:
  ankibug "note text" [--screenshot PATH] [--card ID --note ID]
                       [--source terminal|chat|hotkey] [--kind app|deck|unknown]
  ankibug --from-screenshot PATH "note text"
  ankibug list
  ankibug show ID
  ankibug set ID key=value [key=value ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import capture as capture_mod
from . import schema as schema_mod
from . import store as store_mod

SUBCOMMANDS = ("list", "show", "set")


def _classify_kind(ticket: dict) -> Optional[str]:
    """Best-effort keyword classification at capture time (app vs deck), so
    hotkey/chat tickets don't sit at `kind: unknown` in `ankibug list` until
    ankifix runs. Reuses ankifix's own keyword classifier (ankifix.classify,
    the same one ankifix itself uses at fix time) rather than duplicating the
    keyword lists here. ankifix.classify has no import of ankibug at module
    scope, so this does not create a circular import - verified by reading
    ankifix/classify.py, which only imports ankifix.config.

    Returns 'app' or 'deck', or None if ankifix isn't importable (e.g. a bare
    ankibug install) or classification itself raises - callers keep the
    ticket's existing kind (normally 'unknown') in that case. Old tickets
    already on disk are never touched by this; `unknown` is still a valid,
    accepted `kind` for them.
    """
    try:
        from ankifix.classify import classify as ankifix_classify  # type: ignore
        from ankifix.config import load_config as ankifix_load_config  # type: ignore
    except ImportError:
        return None
    try:
        kind, _reason = ankifix_classify(ticket, ankifix_load_config())
        return kind
    except Exception:  # noqa: BLE001 - classification must never block filing a ticket
        return None


def _read_file_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        print("ankibug: could not read screenshot {!r}: {}".format(path, exc), file=sys.stderr)
        return None


def build_file_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ankibug", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("text", nargs="?", help="the bug note text")
    p.add_argument("--screenshot", metavar="PATH",
                   help="attach an existing screenshot in addition to a live capture")
    p.add_argument("--from-screenshot", metavar="PATH",
                   help="file from chat: attach PATH as the screenshot, sets source=chat")
    p.add_argument("--card", dest="card", type=int, default=None, help="reviewer card id")
    p.add_argument("--note", dest="note_id", type=int, default=None,
                   help="reviewer note id (distinct from the positional note text)")
    p.add_argument("--source", choices=schema_mod.SOURCES, default="terminal")
    p.add_argument("--kind", choices=schema_mod.KINDS, default="unknown")
    return p


def build_list_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="ankibug list")


def build_show_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ankibug show")
    p.add_argument("id")
    return p


def build_set_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ankibug set")
    p.add_argument("id")
    p.add_argument("assignments", nargs="+", help="key=value, e.g. status=fixed")
    return p


def cmd_file(args: argparse.Namespace) -> int:
    log: List[str] = []
    source = "chat" if args.from_screenshot else args.source

    app_info = capture_mod.get_app_info()
    reviewer = {"card_id": args.card, "note_id": args.note_id}

    capture_dict = None
    screenshot_bytes = None
    qa_html = None
    reviewer_state = None

    if args.from_screenshot:
        # Chat hand-off: attach the supplied image; still try a live
        # capture for page/console/reviewer context but never let it
        # overwrite the user-supplied screenshot.
        screenshot_bytes = _read_file_bytes(args.from_screenshot)
        if screenshot_bytes is not None:
            log.append("capture: using supplied screenshot {}".format(args.from_screenshot))
        capture_dict, _live_shot, qa_html, reviewer_state = capture_mod.capture(log=log)
        if capture_dict is not None:
            capture_dict["screenshot_path"] = "screenshot.png" if screenshot_bytes else None
    elif args.screenshot:
        screenshot_bytes = _read_file_bytes(args.screenshot)
        if screenshot_bytes is not None:
            log.append("capture: using supplied screenshot {}".format(args.screenshot))
        capture_dict, live_shot, qa_html, reviewer_state = capture_mod.capture(log=log)
        if screenshot_bytes is None:
            screenshot_bytes = live_shot
        elif capture_dict is not None:
            capture_dict["screenshot_path"] = "screenshot.png"
    else:
        capture_dict, screenshot_bytes, qa_html, reviewer_state = capture_mod.capture(log=log)

    if capture_dict is None:
        print("ankibug: Anki+ not reachable on the CDP port; filing ticket without capture.")

    reviewer["state"] = reviewer_state

    ticket = schema_mod.new_ticket(
        note=args.text,
        source=source,
        kind=args.kind,
        app=app_info,
        reviewer=reviewer,
        capture=capture_dict,
    )
    if ticket["kind"] == "unknown":
        guessed = _classify_kind(ticket)
        if guessed:
            ticket["kind"] = guessed
            schema_mod.validate(ticket)

    tdir = store_mod.write_ticket(
        ticket,
        screenshot_bytes=screenshot_bytes,
        qa_html=qa_html,
        log_text="\n".join(log) if log else None,
    )

    print("ankibug: filed ticket {}".format(ticket["id"]))
    print("  {}".format(tdir))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    tickets = store_mod.list_tickets()
    if not tickets:
        print("no tickets yet")
        return 0
    for t in tickets:
        print("{}  [{}/{}]  {}".format(
            t.get("id"), t.get("kind"), t.get("status"), t.get("note", "")[:70]))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        ticket = store_mod.read_ticket(args.id)
    except (OSError, schema_mod.SchemaError) as exc:
        print("ankibug: could not read ticket {!r}: {}".format(args.id, exc), file=sys.stderr)
        return 1
    print(json.dumps(ticket, indent=2))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    try:
        store_mod.read_ticket(args.id)
    except (OSError, schema_mod.SchemaError) as exc:
        print("ankibug: could not read ticket {!r}: {}".format(args.id, exc), file=sys.stderr)
        return 1

    for assignment in args.assignments:
        if "=" not in assignment:
            print("ankibug: bad assignment {!r}, expected key=value".format(assignment), file=sys.stderr)
            return 1
        key, _, value = assignment.partition("=")
        try:
            store_mod.set_field(args.id, key.strip(), value)
        except schema_mod.SchemaError as exc:
            print("ankibug: {}".format(exc), file=sys.stderr)
            return 1

    print("ankibug: updated {}".format(args.id))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Dispatch manually rather than via argparse subparsers: the default
    # (no-subcommand) form's positional note text can be any free text,
    # including text that happens to collide with a subcommand name, so
    # we only treat argv[0] as a subcommand when it's an exact, bare match.
    if argv and argv[0] in SUBCOMMANDS:
        name, rest = argv[0], argv[1:]
        if name == "list":
            build_list_parser().parse_args(rest)
            return cmd_list(argparse.Namespace())
        if name == "show":
            args = build_show_parser().parse_args(rest)
            return cmd_show(args)
        if name == "set":
            args = build_set_parser().parse_args(rest)
            return cmd_set(args)

    parser = build_file_parser()
    args = parser.parse_args(argv)
    if not args.text:
        parser.error("note text is required, e.g. ankibug \"description of the bug\"")
    return cmd_file(args)


if __name__ == "__main__":
    sys.exit(main())
