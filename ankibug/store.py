"""On-disk ticket store: ~/AnkiTickets/<id>/ + index.md."""

from __future__ import annotations

import base64
import datetime
import json
import os
from typing import Any, Dict, List, Optional

from . import schema as schema_mod

DEFAULT_ROOT = "~/AnkiTickets"


def ticket_dir(ticket_id: str, root: str = DEFAULT_ROOT) -> str:
    return os.path.join(root, ticket_id)


def ticket_json_path(ticket_id: str, root: str = DEFAULT_ROOT) -> str:
    return os.path.join(ticket_dir(ticket_id, root), "ticket.json")


def index_path(root: str = DEFAULT_ROOT) -> str:
    return os.path.join(root, "index.md")


def write_ticket(
    ticket: Dict[str, Any],
    root: str = DEFAULT_ROOT,
    screenshot_bytes: Optional[bytes] = None,
    qa_html: Optional[str] = None,
    log_text: Optional[str] = None,
) -> str:
    """Validate and persist a ticket dict plus any side files.

    Returns the ticket directory path. Never touches the Anki
    collection; this is purely files under `root`.
    """
    schema_mod.validate(ticket)
    tdir = ticket_dir(ticket["id"], root)
    os.makedirs(tdir, exist_ok=True)

    if screenshot_bytes is not None:
        with open(os.path.join(tdir, "screenshot.png"), "wb") as f:
            f.write(screenshot_bytes)

    if qa_html is not None:
        with open(os.path.join(tdir, "qa.html"), "w", encoding="utf-8") as f:
            f.write(qa_html)

    if log_text is not None:
        with open(os.path.join(tdir, "capture.log"), "a", encoding="utf-8") as f:
            f.write(log_text)
            if not log_text.endswith("\n"):
                f.write("\n")

    with open(ticket_json_path(ticket["id"], root), "w", encoding="utf-8") as f:
        json.dump(ticket, f, indent=2, sort_keys=False)
        f.write("\n")

    regenerate_index(root)
    return tdir


def read_ticket(ticket_id: str, root: str = DEFAULT_ROOT) -> Dict[str, Any]:
    path = ticket_json_path(ticket_id, root)
    with open(path, "r", encoding="utf-8") as f:
        ticket = json.load(f)
    schema_mod.validate(ticket)
    return ticket


def list_tickets(root: str = DEFAULT_ROOT) -> List[Dict[str, Any]]:
    if not os.path.isdir(root):
        return []
    tickets = []
    for name in sorted(os.listdir(root)):
        tjson = os.path.join(root, name, "ticket.json")
        if os.path.isfile(tjson):
            try:
                with open(tjson, "r", encoding="utf-8") as f:
                    tickets.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
    tickets.sort(key=lambda t: t.get("id", ""))
    return tickets


def update_status(ticket_id: str, status: str, root: str = DEFAULT_ROOT) -> Dict[str, Any]:
    if status not in schema_mod.STATUSES:
        raise schema_mod.SchemaError(
            "status must be one of {}".format(schema_mod.STATUSES))
    ticket = read_ticket(ticket_id, root)
    ticket["status"] = status
    schema_mod.validate(ticket)
    with open(ticket_json_path(ticket_id, root), "w", encoding="utf-8") as f:
        json.dump(ticket, f, indent=2, sort_keys=False)
        f.write("\n")
    regenerate_index(root)
    return ticket


def set_field(ticket_id: str, dotted_key: str, value: Any, root: str = DEFAULT_ROOT) -> Dict[str, Any]:
    """Set a (possibly nested, dotted) field on a ticket, e.g. 'status' or 'fix.summary'."""
    ticket = read_ticket(ticket_id, root)
    parts = dotted_key.split(".")
    node = ticket
    for p in parts[:-1]:
        if node.get(p) is None:
            raise schema_mod.SchemaError(
                "cannot set nested field {!r}: {!r} is null".format(dotted_key, p))
        node = node[p]
    node[parts[-1]] = value
    schema_mod.validate(ticket)
    with open(ticket_json_path(ticket_id, root), "w", encoding="utf-8") as f:
        json.dump(ticket, f, indent=2, sort_keys=False)
        f.write("\n")
    regenerate_index(root)
    return ticket


def _escape_pipes(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _truncate(text: str, n: int = 60) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def regenerate_index(root: str = DEFAULT_ROOT) -> str:
    os.makedirs(root, exist_ok=True)
    tickets = list_tickets(root)
    lines = [
        "# AnkiTickets index",
        "",
        "| id | created | source | kind | status | note |",
        "|---|---|---|---|---|---|",
    ]
    for t in tickets:
        lines.append(
            "| {id} | {created} | {source} | {kind} | {status} | {note} |".format(
                id=t.get("id", ""),
                created=t.get("created", ""),
                source=t.get("source", ""),
                kind=t.get("kind", ""),
                status=t.get("status", ""),
                note=_escape_pipes(_truncate(t.get("note", ""))),
            )
        )
    content = "\n".join(lines) + "\n"
    with open(index_path(root), "w", encoding="utf-8") as f:
        f.write(content)
    return index_path(root)
