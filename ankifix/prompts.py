"""Render the Claude prompts for app and deck tickets."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

from .config import Config
from .tickets import resolve_rel, ticket_dir

QA_LIMIT = 20000
TEXT_LIMIT = 8000


def _read(path: Optional[Path], limit: int) -> str:
    if not path or not path.is_file():
        return "(not captured)"
    s = path.read_text(errors="replace")
    if len(s) > limit:
        s = s[:limit] + f"\n... [truncated, {len(s)} chars total]"
    return s


def _clip(s: Optional[str], limit: int) -> str:
    if not s:
        return "(not captured)"
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated, {len(s)} chars total]"


def claude_md_pointers(repo_dir: Path) -> str:
    p = Path(repo_dir) / "CLAUDE.md"
    if not p.is_file():
        return "  - (no CLAUDE.md found)"
    heads = []
    in_code = False
    for line in p.read_text(errors="replace").splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not in_code and re.match(r"#{2,3} ", line):
            heads.append("  - " + line.strip())
    return "\n".join(heads) or "  - (CLAUDE.md has no sections)"


def _common(ticket: Dict[str, Any], cfg: Config) -> Dict[str, str]:
    tdir = ticket_dir(cfg.tickets_dir, ticket["id"])
    cap = ticket.get("capture") or {}
    qa_path = resolve_rel(tdir, cap.get("qa_html_path"))
    shot = resolve_rel(tdir, cap.get("screenshot_path"))
    errs = cap.get("console_errors") or []
    return {
        "ticket_id": ticket["id"],
        "ticket_dir": str(tdir),
        "note": ticket.get("note") or "(empty)",
        "expected": ticket.get("expected") or "(not given)",
        "ticket_json": json.dumps(ticket, indent=2, ensure_ascii=False),
        "screenshot_path": str(shot) if shot else "(no screenshot)",
        "qa_html_path": str(qa_path) if qa_path else "(no qa.html)",
        "qa_html": _read(qa_path, QA_LIMIT),
        "qa_limit": str(QA_LIMIT),
        "visible_text": _clip(cap.get("visible_text"), TEXT_LIMIT),
        "console_errors": "\n".join(f"  - {e}" for e in errs) or "  - (none)",
        "co_author": cfg.co_author,
    }


def render_app(ticket: Dict[str, Any], cfg: Config) -> str:
    wt = cfg.worktree_path(ticket["id"])
    ctx = _common(ticket, cfg)
    ctx.update(
        worktree=str(wt),
        app_repo=str(cfg.app_repo),
        branch=cfg.branch_name(ticket["id"]),
        base_branch=cfg.app_base_branch,
        remote=cfg.app_remote,
        node_path=cfg.node_path,
        # the worktree may not exist yet (dry run); fall back to the main checkout's copy
        claude_md_pointers=claude_md_pointers(wt if (wt / "CLAUDE.md").is_file() else cfg.app_repo),
        test_commands="\n".join(f"- `{c}`" for c in cfg.app_test_commands),
    )
    return Template(Path(cfg.prompt_app).read_text()).safe_substitute(ctx)


def deck_build_dir(ticket: Dict[str, Any], cfg: Config) -> Path:
    db = ticket.get("deck_build") or {}
    d = db.get("course_dir")
    if not d:
        return Path(cfg.deck_build_dir)
    p = Path(d).expanduser()
    if (p / "build_deck.py").is_file():
        return p
    for sub in ("Anki/build", "build"):
        if (p / sub / "build_deck.py").is_file():
            return p / sub
    return p


def find_course_deck_md(build_dir: Path) -> Optional[Path]:
    for p in [build_dir, *build_dir.parents][:4]:
        if (p / "COURSE_DECK.md").is_file():
            return p / "COURSE_DECK.md"
    return None


def _strip(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return " ".join(html.unescape(s).split()).lower()


def match_build_notes(ticket: Dict[str, Any], build_out: Dict[str, Any], limit: int = 3) -> List[str]:
    """Best-effort: which build_out.json keys does the captured card show?"""
    cap = ticket.get("capture") or {}
    tdir_text = " ".join(
        [cap.get("visible_text") or "", ticket.get("note") or ""]
    )
    hay = _strip(tdir_text)
    scored = []
    for key, entry in build_out.items():
        fields = entry.get("fields") or []
        score = 0
        if re.search(r"(?<![A-Za-z0-9-])" + re.escape(key.lower()) + r"(?![A-Za-z0-9-])", hay):
            score += 100
        for f in fields[1:3]:  # Question, QuestionImage/Text
            q = _strip(f)
            if len(q) >= 25 and q[:60] in hay:
                score += 10 + len(q[:60]) // 10
        if score:
            scored.append((score, key))
    scored.sort(reverse=True)
    return [k for _, k in scored[:limit]]


def note_fields_block(ticket: Dict[str, Any], build_dir: Path) -> str:
    bo = build_dir / "build_out.json"
    if not bo.is_file():
        return "(build_out.json not found; locate the card yourself)"
    try:
        data = json.loads(bo.read_text())
    except ValueError:
        return "(build_out.json unreadable; locate the card yourself)"
    keys = match_build_notes(ticket, data)
    if not keys:
        return (
            f"(could not match the captured card to a build_out.json key automatically; "
            f"search `{bo}` for text from the visible text above)"
        )
    parts = []
    for k in keys:
        parts.append(f"### {k}\n\n```json\n{json.dumps(data[k], indent=2, ensure_ascii=False)}\n```")
    return "Best matches (verify which one is the ticket's card):\n\n" + "\n\n".join(parts)


def render_deck(ticket: Dict[str, Any], cfg: Config) -> str:
    bdir = deck_build_dir(ticket, cfg)
    cdm = find_course_deck_md(bdir)
    ctx = _common(ticket, cfg)
    ctx.update(
        build_dir=str(bdir),
        course_deck_md=str(cdm) if cdm else "(COURSE_DECK.md not found near the build dir)",
        deck_python=cfg.deck_python,
        note_fields=note_fields_block(ticket, bdir),
    )
    return Template(Path(cfg.prompt_deck).read_text()).safe_substitute(ctx)


def render(ticket: Dict[str, Any], kind: str, cfg: Config) -> str:
    return render_app(ticket, cfg) if kind == "app" else render_deck(ticket, cfg)
