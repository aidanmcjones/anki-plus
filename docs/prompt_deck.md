# ankifix: fix a course-deck card bug headless

You are running non-interactively under `ankifix`. Nobody will answer
questions; make reasonable calls and record them in your final report.

## Where you are

- Working directory: `${build_dir}` (the course deck build directory).
- Course conventions: `${course_deck_md}`. Read sections **7 (Card
  conventions)** and **8 (High-yield pass)** before editing anything; they
  are binding (verbatim answers, fb- class prefix, cloze rules, display
  contract, no repeated answers).
- The ticket directory `${ticket_dir}` is readable (qa.html, screenshot) but
  do not modify it.

## The ticket

User's note: ${note}
Expected: ${expected}

Full ticket.json:

```json
${ticket_json}
```

- Screenshot: ${screenshot_path}  (use the Read tool on the PNG)
- Console errors:
${console_errors}

Visible text at capture time:

```text
${visible_text}
```

qa.html (truncated to ${qa_limit} chars):

```html
${qa_html}
```

## The note's current built fields (from build_out.json)

${note_fields}

## Required procedure (output contract)

1. Identify the card(s) involved (the `ID` field / build_out.json key) and
   the source of the bad content: `cards/cards_c*.yaml` (her verbatim text),
   `cards/lgoals.yaml`, or `cards/overrides.yaml` (delete / set / draw).
   Prefer `cards/overrides.yaml` for changes so the per-class YAML stays
   verbatim; edit `build_deck.py` templates/CSS only when the bug is in
   rendering shared by many cards.
2. Rebuild: `${deck_python} build_deck.py`. The preflight gate must print
   `RESULT: PASS`. Read the warnings in `preflight_report.txt`. If it fails,
   fix and rebuild; never hand over a failing build.
3. Work out the exact command the user must run **with Anki+ closed** to
   push the fix into the live collection, e.g.
   `cd ~/dev/anki && out/pyenv/bin/python "${build_dir}/apply_highyield.py" --repatch=<ID>,<ID>`
   (or `apply_styles.py` for template/CSS-only changes; see §8).

## Hard rules

- NEVER run `apply_*.py`, `fix_collection.py`, or anything that opens
  `collection.anki2`. Never touch `~/Library/Application Support/Anki2/`.
  Never start, stop, or restart Anki+.
- Do not delete files or the `_backup*` directories.
- No new facts: answers stay her words (§2, §7).

## Final message

End your final message with a fenced block tagged `ankifix-result`
containing one JSON object, exactly this shape:

```ankifix-result
{"status": "fixed or failed", "tests_green": true, "tests": ["build_deck.py preflight: RESULT: PASS"], "card_ids": ["C4-Q10"], "apply_command": "cd ~/dev/anki && out/pyenv/bin/python \".../apply_highyield.py\" --repatch=C4-Q10", "summary": "2-4 sentences: what was wrong, what you changed, in which file"}
```

Style rule: never use em dashes anywhere you write (code comments, commit messages, docs, summaries). Use a comma, a colon, or a new sentence instead.

Pull requests: never open a pull request (no `gh pr create`, no PR via the API). Push your fix branch only; the maintainer lands it on the one open PR for the base branch.
