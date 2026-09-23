"""Ticket schema (v1): validation and construction helpers.

See SCHEMA.md at the repo root for the authoritative field-by-field
description. This module is the executable version of that document.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

SOURCES = ("hotkey", "terminal", "chat")
STATUSES = ("new", "fixing", "fixed", "needs-review", "failed", "wontfix")
KINDS = ("app", "deck", "unknown")
REVIEWER_STATES = ("question", "answer", None)


class SchemaError(ValueError):
    """Raised when a ticket dict fails validation."""


def slugify(text: str, max_len: int = 40) -> str:
    """Lowercase, collapse non-alphanumerics to single hyphens, trim."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if not text:
        return "ticket"
    return text[:max_len].strip("-") or "ticket"


def make_id(note: str, when: Optional[datetime.datetime] = None) -> str:
    when = when or datetime.datetime.now()
    return "{}-{}".format(when.strftime("%Y%m%d-%H%M%S"), slugify(note))


def _default_app_info() -> Dict[str, Any]:
    return {
        "anki_version": None,
        "addon_repo": "~/dev/anki-design",
        "branch": None,
        "commit": None,
        "dirty": None,
    }


def _default_reviewer_info() -> Dict[str, Any]:
    return {
        "state": None,
        "card_id": None,
        "note_id": None,
        "notetype": None,
        "deck": None,
        "template_ord": None,
    }


def new_ticket(
    note: str,
    source: str = "terminal",
    kind: str = "unknown",
    app: Optional[Dict[str, Any]] = None,
    reviewer: Optional[Dict[str, Any]] = None,
    deck_build: Optional[Dict[str, Any]] = None,
    capture: Optional[Dict[str, Any]] = None,
    fix: Optional[Dict[str, Any]] = None,
    when: Optional[datetime.datetime] = None,
    ticket_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a fresh, schema-valid ticket dict.

    Does not write anything to disk; see store.py for that.
    """
    when = when or datetime.datetime.now().astimezone()
    ticket = {
        "schema": SCHEMA_VERSION,
        "id": ticket_id or make_id(note, when),
        "created": when.isoformat(timespec="seconds"),
        "source": source,
        "note": note,
        "status": "new",
        "kind": kind,
        "app": {**_default_app_info(), **(app or {})},
        "reviewer": {**_default_reviewer_info(), **(reviewer or {})},
        "deck_build": deck_build,
        "capture": capture,
        "fix": fix,
    }
    validate(ticket)
    return ticket


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def validate(ticket: Dict[str, Any]) -> None:
    """Raise SchemaError if `ticket` does not conform to schema v1."""
    _require(isinstance(ticket, dict), "ticket must be a dict")

    _require(ticket.get("schema") == SCHEMA_VERSION,
              "schema field must be {}".format(SCHEMA_VERSION))

    for field in ("id", "created", "source", "note", "status", "kind"):
        _require(field in ticket, "missing required field: {}".format(field))
        _require(isinstance(ticket[field], str), "{} must be a string".format(field))

    _require(ticket["source"] in SOURCES,
              "source must be one of {}".format(SOURCES))
    _require(ticket["status"] in STATUSES,
              "status must be one of {}".format(STATUSES))
    _require(ticket["kind"] in KINDS,
              "kind must be one of {}".format(KINDS))

    try:
        datetime.datetime.fromisoformat(ticket["created"])
    except ValueError as exc:
        raise SchemaError("created is not valid ISO 8601: {}".format(exc))

    app = ticket.get("app")
    _require(isinstance(app, dict), "app must be a dict")
    for field in ("anki_version", "addon_repo", "branch", "commit"):
        _require(field in app, "app missing field: {}".format(field))
    _require("dirty" in app, "app missing field: dirty")
    _require(app["dirty"] is None or isinstance(app["dirty"], bool),
              "app.dirty must be bool or null")

    reviewer = ticket.get("reviewer")
    _require(isinstance(reviewer, dict), "reviewer must be a dict")
    for field in ("state", "card_id", "note_id", "notetype", "deck", "template_ord"):
        _require(field in reviewer, "reviewer missing field: {}".format(field))
    _require(reviewer["state"] in REVIEWER_STATES,
              "reviewer.state must be one of {}".format(REVIEWER_STATES))

    deck_build = ticket.get("deck_build")
    _require(deck_build is None or isinstance(deck_build, dict),
              "deck_build must be a dict or null")
    if isinstance(deck_build, dict):
        for field in ("course_dir", "build_marker"):
            _require(field in deck_build,
                      "deck_build missing field: {}".format(field))

    capture = ticket.get("capture")
    _require(capture is None or isinstance(capture, dict),
              "capture must be a dict or null")
    if isinstance(capture, dict):
        for field in ("pages", "qa_html_path", "visible_text",
                       "console_errors", "screenshot_path", "captured_at"):
            _require(field in capture, "capture missing field: {}".format(field))
        _require(isinstance(capture["pages"], list), "capture.pages must be a list")
        _require(isinstance(capture["console_errors"], list),
                  "capture.console_errors must be a list")

    fix = ticket.get("fix")
    _require(fix is None or isinstance(fix, dict), "fix must be a dict or null")
    if isinstance(fix, dict):
        for field in ("branch", "commits", "tests", "log_path",
                       "started", "finished", "summary"):
            _require(field in fix, "fix missing field: {}".format(field))
        _require(isinstance(fix["commits"], list), "fix.commits must be a list")
        _require(isinstance(fix["tests"], list), "fix.tests must be a list")


def is_valid(ticket: Dict[str, Any]) -> bool:
    try:
        validate(ticket)
        return True
    except SchemaError:
        return False
