"""Ticket I/O and index.md regeneration (contract: ankibug SCHEMA.md v1)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUSES = ("new", "fixing", "fixed", "failed", "wontfix")
KINDS = ("app", "deck", "unknown")


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ticket_dir(tickets_dir: Path, ticket_id: str) -> Path:
    return Path(tickets_dir) / ticket_id


def load_ticket(tickets_dir: Path, ticket_id: str) -> Dict[str, Any]:
    path = ticket_dir(tickets_dir, ticket_id) / "ticket.json"
    if not path.is_file():
        raise FileNotFoundError(f"no ticket at {path}")
    return json.loads(path.read_text())


def save_ticket(tickets_dir: Path, ticket: Dict[str, Any]) -> Path:
    """Atomic write of ticket.json, then regenerate index.md."""
    try:
        from ankibug.schema import validate  # type: ignore
    except ImportError:
        validate = None
    if validate is not None:
        validate(ticket)
    d = ticket_dir(tickets_dir, ticket["id"])
    d.mkdir(parents=True, exist_ok=True)
    path = d / "ticket.json"
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".ticket.", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(ticket, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    write_index(tickets_dir)
    return path


def list_tickets(tickets_dir: Path) -> List[Dict[str, Any]]:
    out = []
    root = Path(tickets_dir)
    if not root.is_dir():
        return out
    for tj in sorted(root.glob("*/ticket.json")):
        try:
            out.append(json.loads(tj.read_text()))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda t: (str(t.get("created") or ""), str(t.get("id"))))  # queue order
    return out


def _cell(value: Any, limit: Optional[int] = None) -> str:
    s = "" if value is None else str(value)
    s = " ".join(s.split())
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s.replace("|", "\\|")


def render_index(tickets: List[Dict[str, Any]]) -> str:
    lines = [
        "# AnkiTickets index",
        "",
        "| id | created | source | kind | status | note |",
        "|---|---|---|---|---|---|",
    ]
    for t in tickets:
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                _cell(t.get("id")),
                _cell(t.get("created")),
                _cell(t.get("source")),
                _cell(t.get("kind")),
                _cell(t.get("status")),
                _cell(t.get("note"), 60),
            )
        )
    return "\n".join(lines) + "\n"


def write_index(tickets_dir: Path) -> Path:
    """Regenerate index.md. Delegates to ankibug's writer when importable so the
    two tools never fight over the file's format; the local renderer is the fallback."""
    try:
        from ankibug.store import regenerate_index  # type: ignore
    except ImportError:
        regenerate_index = None
    if regenerate_index is not None:
        return Path(regenerate_index(str(tickets_dir)))
    root = Path(tickets_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "index.md"
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".index.", suffix=".md")
    with os.fdopen(fd, "w") as fh:
        fh.write(render_index(list_tickets(root)))
    os.replace(tmp, path)
    return path


def resolve_rel(tdir: Path, p: Optional[str]) -> Optional[Path]:
    """capture paths are relative to the ticket dir (absolute also accepted)."""
    if not p:
        return None
    pp = Path(p).expanduser()
    return pp if pp.is_absolute() else tdir / pp
