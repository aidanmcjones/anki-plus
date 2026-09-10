# Anki Design configuration

Every option here is also in **Tools → Anki Design Settings…** (or the
sidebar's Settings row), which is the recommended way to change them. Values
in this file are the raw form the settings page writes.

## Appearance
- **theme** — `"system"`, `"light"`, or `"dark"`.
- **accent** — hex colour for links, the progress bar, and other accents.
  Example: `"#6c8cff"`.
- **background_light** / **background_dark** — page colour override per
  theme. `""` keeps the default paper (light) / ink (dark); a hex such as
  `"#ffffff"` replaces it.
- **density** — `"compact"`, `"cozy"`, or `"comfortable"`.
- **font_serif** / **font_sans** — optional display fonts prepended to the
  built-in stacks (e.g. `"Iowan Old Style"`). `""` uses the defaults.

## Home page
- **sidebar_nav** — the left rail replaces Anki's top toolbar. The inline
  windows below need it.
- **show_today** — the today panel (cards · minutes + per-hour bars).
- **show_streak** — the streak counter above the heatmap.
- **show_heatmap**, **heatmap_weeks**, **heatmap_palette** — the review
  heatmap, its minimum width in weeks (`53` ≈ a year), and its colour
  (`"accent"`, `"green"`, `"teal"`, `"violet"`, `"rose"`, `"amber"`).
- **hide_bottom_on_decks** / **hide_bottom_on_overview** — hide Anki's
  native bottom strip on those screens.

## Deck list
- **deck_tree_startup** — `"remember"` keeps each deck's own open/closed
  state (Anki's synced flag), `"expanded"` opens everything on launch,
  `"collapsed"` closes every parent on launch.
- **deck_drag_move** — drag a deck onto another to nest it (or onto the
  top-level zone). "Move to…" in the deck menu is always available.
- **single_deck_hero** — with one top-level deck, show it as a big card with
  its sub-decks listed beneath.
- **skip_overview** — clicking a deck starts studying right away. `false`
  opens Anki's overview page first.

## Reviewer
- **reviewer_card_width** — `"narrow"`, `"medium"`, `"wide"`, or `"full"`.
  **This no longer sizes the card area.** Card content now always fills
  the window and grows with it, because a fixed measure clipped anything
  that wasn't prose — tables, diagrams, screenshots. The value still
  sizes the answer-button strip.
- **reviewer_font_size** — `"small"`, `"medium"`, `"large"`, or `"x-large"`.
- **reviewer_card_styling** — apply Anki Design typography and colours to
  the card body. `false` keeps your note type's own CSS untouched.
- **reviewer_answer_buttons** — `"intervals"` (interval chips under the
  answer) or `"native"` (Anki's Again / Hard / Good / Easy bar).
- **show_progress** — progress strip across the top of the reviewer.
- **click_to_reveal** — clicking the card shows the answer.
- **press_feedback** — the bloom animation when grading.
- **reviewer_hide_answer** — a quiet "Hide Answer" button in the bottom-right
  corner while the answer is showing (also `H`). Returns to the question
  side of the *same* card — nothing is graded and nothing is scheduled — so
  you can try recalling it again. `false` removes the button and the key.
- **reviewer_prev_card** — a "‹ Previous" button in the bottom-left corner,
  on both sides of the card (also `P`). Steps back to the card you just
  answered so you can change the grade: it runs Anki's own review undo, so
  that card's interval, due date and ease are restored exactly as they were
  and it returns in its question state to be graded again. Press it again to
  keep walking back. Dimmed and inert when there's nothing to go back to —
  at the start of a session, or when the last thing you did was something
  other than answering (adding a note, changing decks). `false` removes the
  button and the key.
- **inline_edit** — `E` edits fields in place; `false` opens Anki's editor.
- **reviewer_menu_extras** — **Randomize Set** and **Cue…** in the
  reviewer's **More** menu, and **Randomize** on each deck's gear menu.
  Randomize shuffles that deck's new cards into a random order in place —
  nothing changes decks, nothing new is created, and Ctrl+Z puts the old
  order back; from the reviewer it also draws a fresh card straight away.
  **Cue…** lists the tags actually present in the deck you're studying
  and turns any of them into an immediate study queue (picking a parent
  tag cues its children with it); that one builds a rescheduling filtered
  deck named `Study: <tag>`, so your answers count exactly as normal
  reviews do, and re-cueing refreshes that deck instead of making
  another. Suspended cards are left out of both. `false` leaves Anki's
  More menu untouched and drops Randomize from the gear menu.
- **reviewer_text_lookup** — text on a card is selectable (as it is in
  stock Anki), and right-clicking a selection offers **Search Google
  for "…"**, which opens your default browser. Works in the reviewer and
  in the Browse tab's preview pane. `false` removes the menu item;
  selecting text still works.

## Deck deadlines
- **deck_deadlines** — **Memorize by…** on a deck's gear menu: pick a date
  and every interval in that deck is capped at the days remaining, so no
  answer — Easy included — can schedule a card past the deadline. The menu
  item then shows the date and the countdown ("Memorize by Sep 15 — 6d").
  Anki caps intervals per *preset*, and presets are shared between decks,
  so a deadlined deck is moved onto its own copy of its preset
  (`<preset> — deadline`) and the decks that shared the original are left
  alone; clearing the deadline moves it back. The cap is recalculated
  once a day, so it shrinks as the date approaches, and the deck is
  restored when the date passes. The dialog also checks whether the new
  cards can be *seen* in time — "212 new cards in 6 days needs 36/day —
  this deck's limit is 20" — and offers to raise the limit in one click.
  `false` removes the menu item and stops enforcing any deadline already
  set.

  A date in the **past** is allowed, and is how you record that a deck was
  learned on time — the gear row then reads "Memorized by Sep 8 —
  passed ✓". The interval cap comes off, and the dialog asks what the deck
  should do from there; the choice is remembered per deck:
  - **Maintain long-term** (default) — reviews carry on as normal. Cards
    coming due after the deadline are memory upkeep, not a missed target;
    that is how spaced repetition holds something you already know.
  - **Pause reviews** — the deck stops presenting anything, by setting its
    per-day review and new limits to 0. Reversible, and no card is
    suspended or otherwise altered: switch back to Maintain, or clear the
    deadline, to resume.

## Windows
- **cmdk** — the ⌘K / Ctrl+K command palette.
- **embed_add**, **embed_browse**, **embed_stats**, **embed_settings** — open
  those inside the main window (needs `sidebar_nav`); `false` uses Anki's
  separate windows.
- **restyle_addcard** — the redesigned Add window. `false` restores Anki's
  stock Add window.
- **congrats_redesign** — the redesigned finished-deck page.
- **silent_sync** — sync progress in the sidebar instead of a dialog.

## Browse
- **browse_restyle** — paint the Browser's Qt chrome (card table, column
  headers, sidebar tree, search bar, splitters, scrollbars) in the Anki
  Design palette. `false` leaves Anki's own browser styling alone.
- **browse_sidebar_minimal** — show only **Decks** and **Tags** in the
  Browse sidebar. `false` restores Anki's full tree (Saved Searches,
  Today, Flags, Card State, Note Types). Nothing is lost with it on: the
  hidden rows are all searches you can type — `is:due`, `flag:1`,
  `added:1`, `note:Basic`, `deck:current` — and the menu actions that
  create saved searches keep working.
- **browse_render_preview** — split the Browse editor pane vertically and
  render the selected card on top, exactly as the reviewer renders it
  (note type CSS, cloze, images, and your Reviewer settings). Click the
  card, or the Question / Answer switch, to flip sides. `false` gives the
  fields the whole pane back, as in stock Anki. Needs `embed_browse`: the
  pane is built when Browse opens as a tab. With Browse in its own window
  you still have Anki's own preview on `Ctrl+Shift+P`.
- **restyle_browse_editor** — apply the Add window's editor styling to
  the Browse fields pane. `false` keeps Anki's stock editor there.

Changes take effect the next time the relevant screen is drawn (return to
the deck list / start a review). The four **Browse** options above are
read when the Browse tab opens, so close and reopen it after changing
them. Restarting Anki is never required.
