# Changelog

All notable changes to Anki Design are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is semver-ish.

## [Unreleased]
### Added
- **Drag a deck to resort the list.** Anki only ever sorts decks by name,
  so "put this one at the top" meant renaming it. Now a deck dropped on
  the top or bottom edge of a sibling lands just above or below it: the
  top edge of the first sibling puts it first, the bottom edge of the
  last puts it last, and an accent line shows where it will go. The
  middle of a row still nests, as it did. The order is the add-on's own
  (`deck_order` in the config, per parent); decks you never moved keep
  their name order after the ones you did, and the congrats "Keep going"
  list follows the same order.

### Fixed
- **Full screen title bar reveals like Safari's.** In macOS full screen the
  "Anki+" bar used to slide over the sidebar wordmark; the first fix made
  it flicker, the second reserved a dark strip across the top of the
  scene. Now, while full screen, the window carries an empty auto-hiding
  toolbar, so AppKit itself slides the content down with the bar and back
  up again, in the bar's own animation. No strip, no timer, no cursor
  polling (`fullscreen_inset.py`).
- **Deck drag-to-reorder now works in the app.** Two things stopped it
  that the synthetic-event tests could not see: Chromium (so QtWebEngine)
  cancels a drag whose `dragstart` inserts the "top level" drop zone, so
  every nested deck's drag ended the instant it began; and Anki's
  webview drops every `drop` event on the deck browser (`allow_drops` is
  reset by each render), so even a top-level drag never reached the page.
  The zone now appears a tick later, and `webview_drops.py` lets drags
  that start inside the page through (files from Finder stay blocked).
- **Dragging cards in Browse visibly moves them.** Cards dropped on a
  sidebar deck now leave the list you are browsing (the table re-runs its
  search), a press-and-pull on an unselected row drags that card, and a
  drop on a review card's row says that only new cards have a queue
  position instead of showing a silent "no entry" cursor.
- **Dragging cards between rows in Browse shows a line, and moves the
  whole selection there.** The drop target was a box drawn around the
  hovered row, which read as "onto this card", and the drop always put
  the cards before it. Now an accent line sits on the row's top edge
  while the cursor is in its upper half and on its bottom edge in the
  lower half, and on release every selected new card lands at that line
  in its queue order: before or after the row, including a hovered row
  that is itself part of the selection (it used to be left out, so the
  others landed in front of it and the group came out reordered). The
  confirmation counts every card moved. A drop on the group's own rows
  changes nothing and says nothing.
- **Sweep-selecting cards in Browse works again.** Pressing on a card and
  pulling down (or up) the list is how the table selects a run of rows,
  and the card drag had taken that gesture for itself: it started a drag
  of the one pressed card, so nothing was selected. A sweep along the
  list from an unselected row now goes to the table, which selects the
  rows it crosses; the group is then dragged by pressing on any of its
  rows. A sideways pull on an unselected row still drags that one card.

### Changed
- **A deadline that has passed now pauses its deck by default.** Setting
  "Memorize by…" and then being handed 385 reviews in those decks the
  evening the date passed is the feature contradicting itself — a deadline
  that keeps presenting cards afterwards is a label, not a deadline. The
  post-deadline default moves from *Maintain long-term* to *Pause
  reviews*, as the new `deadline_passed_mode` config key (`"pause"` |
  `"maintain"`); set it to `"maintain"` for the old behaviour. A deck you
  answered for yourself in the dialog keeps its own answer — the key only
  fills in the blank, and it fills it in live, so every deck that never
  chose follows it without any state being rewritten. The dialog now
  pre-selects it too. Pausing is what it always was: that deck's own
  per-day limits set to 0, reversible, nothing suspended. Deadlines that
  had already passed under the old default are moved across once, on the
  first launch after this change, so decks that went quiet before it
  shipped are covered too.
- **The Browse sidebar picks several decks without a mode.** Anki put
  multi-select behind a two-button tool row above the tree — Search, or
  Select — which is a mode you have to know exists, switch into and
  switch back out of, guarding behaviour that costs nothing to leave on.
  The tool row is gone. The tree is always multi-selectable: ⌘-click adds
  a deck, ⇧-click extends a run, and a plain click still just searches
  the deck you clicked (the one thing the Search tool did that Select
  didn't, put back by hand). Dragging any selected deck onto another
  moves the whole selection in a single undoable step, and dragging from
  the empty space below the last deck lassos a rubber band across rows.
  Right-click acts on everything selected: **Move to…** lists every deck
  the selection could go into, plus *Top level*, for when the destination
  is scrolled somewhere a drag can't reach; **New subsection…** opens
  with those decks already ticked, filed under the deepest deck they
  share. Deck ids and card counts are untouched by any of it — these are
  Anki's own reparent operations. `browse_sidebar_multiselect: false`
  restores the tool row.
- **Decks go by their title.** Clicking a deck in the Browse sidebar
  filled the search box with `"deck:Fundamentals of Biochemistry::Amino
  Acids"`, quotes and all, and the Deck column repeated that full path on
  every row: a deck's name is its title, "Amino Acids", the way the
  sidebar and the deck list already show it. Now the box reads the title
  and the column shows it, with the path as the tooltip. The real query
  is untouched underneath: it is what runs, what Enter re-runs, what
  ⌘-click and ⇧-click combine with, and what **Create Filtered Deck** and
  **Save Current Search** are handed; type anything in the box and what
  you typed is the search again. The reviewer header, the Stats title and
  the Add screen's deck picker use the title too. `clean_deck_names:
  false` puts the paths back.

### Fixed
- **A deck stranded on a "— deadline" preset parked at 0/day shows its
  cards again.** An older build paused a deck by zeroing its *preset's*
  per-day limits; the resume path for that only fires for a deck that
  still has deadline bookkeeping to resume from, so a deck whose deadline
  was cleared while it was paused that way sat on a clone preset offering
  0 new and 0 reviews a day, permanently, with nothing left in the
  codebase that would ever look at it again. A one-time pass restores any
  such clone's per-day limits from the preset it was cloned from (or the
  remembered originals, when there are any) and leaves anything it has no
  evidence for alone.
- **Images can be added to a field again, and resized there.** Two separate
  faults made the embedded editors — the Browse pane's pencil mode and the
  Add tab — unable to take a picture. In the Browse pane the attach button
  wasn't there at all: our JS moved the settings item to the end of the
  toolbar with `appendChild`, and moving a node Svelte owns invalidates the
  anchors it renders against, so on the next re-render (the Browse editor
  re-renders on every row change) the whole attach / record / equations
  group was destroyed and the image-occlusion buttons were drawn in its
  place. The reorder is now pure CSS `order`, applied to the button groups
  rather than the `display: contents` items that were silently ignoring it.
  Second, in both editors the picker ran, the file really was copied into
  `collection.media`, and no image ever appeared: Anki's editor finishes an
  attach on the field's *next* focus event, and asks its parent window to
  activate to produce one. Our Add and Browse shells are hidden windows
  whose widgets live inside the main window, so that activation did
  nothing and the focus event never came. They now hand the activation to
  the window their widgets are actually in. Pasting an image and dragging
  a file onto a field were unaffected and keep working.

  Resizing was never broken, only hidden behind Anki's own **Shrink
  Images** default: click an image and the frame says "double-click to
  expand", because until you do, the corner handles are deliberately inert.
  Expanded, dragging a corner writes `width=` onto the image and the size
  is saved with the note — see the new *Images in fields* section of
  `config.md`, including how a set width interacts with the card's
  `max-width: 100%` cap.

### Changed
- **The Due / New / Learning queues are transient.** Clicking one of the
  sidebar totals gathers that queue *from scratch* at that moment and drops
  you into it; leaving the reviewer sends every card straight back to the
  deck it lives in. Previously the queue stayed built, and since a card can
  only be in one filtered deck at a time, whichever total you clicked last
  quietly held the collection hostage: the deck list under-counted, the
  Browser filed those cards under `Study: Due`, and the next total you
  clicked couldn't see them. Now, at rest, the queues hold nothing and are
  hidden from both the deck list and the Browser's deck tree — cards live
  in their own decks, and every click reflects the collection as it stands.
  The queue is also capped at the number printed on the total you clicked:
  "New 65" hands you 65 cards, not every unseen card in the collection. A
  deck paused by a passed deadline stays paused — filtered decks ignore
  per-day limits, so those decks are now excluded from the gather by name.
- **The reviewer's card area fills the window.** It used to be capped at
  a reading measure (Settings → Reviewer → Card width — 780px at Medium).
  That's a good rule for prose and a bad one for everything else: a
  seven-column table, a wide diagram or a screenshot got squeezed into
  that column in the middle of a wide window and then, because the card
  area scrolls, was cut off on *both* sides. Cards of every shape now use
  the full window minus the page gutter and grow as you resize it. Images
  scale to the available width, preformatted text wraps, and wide tables
  shrink to fit rather than scrolling. Settings → Reviewer → Card width
  no longer sizes the card area. The Browse preview pane follows the same
  rules, fitted to the pane instead of the window.

### Added
- **Randomize** — shuffles a deck's new cards into a random order, in
  place. It's on the deck's gear menu, and in the reviewer's More menu as
  **Randomize Set**, where it shuffles the deck you're studying and draws
  a fresh card straight away. Nothing changes decks and nothing new is
  created: it rewrites the same card positions the Browser's Reposition
  dialog does, so Ctrl+Z puts the old order back.
- **Cue…** in the reviewer's More menu lists the tags actually present in
  the deck you're studying — nested tags as submenus, and picking a
  parent cues its children with it — and turns any of them into an
  immediate study queue. Cueing a subset needs somewhere for the
  selection to live, so this one does build a rescheduling filtered deck;
  answers still count as normal reviews. Like the sidebar totals it is
  transient: the cards go home the moment you leave the reviewer, and
  building one first sends home the cards in the previous queue and in the
  filtered deck you're currently studying — a card can only be in one
  filtered deck at a time, and cards stuck in another are invisible to a
  new one. Cards locked in a filtered deck you're *not* studying are left
  alone, and the message names that deck instead of claiming there's
  nothing to study. Switch both off with **reviewer_menu_extras**.
- **The Browse tab now looks like the rest of the app.** The card table,
  column headers, sidebar tree, search bar, splitters and scrollbars are
  painted from the same palette as the deck list — paper background,
  hairline rules, flat uppercase column labels, a quiet accent wash for
  the selected row, and no Qt button gradients anywhere. Switch off with
  **browse_restyle**.
- **The Browse right-hand pane leads with the card.** One pane, two
  modes. It opens on the selected card drawn exactly as the reviewer
  draws it (note-type CSS, cloze holes, images, MathJax, your reviewer
  typography settings) — front and back together, the whole card, with
  the note's tags in a strip underneath it. The pencil in the pane's
  header (or ⌘E / Ctrl+E) swaps the same space for the fields editor and
  becomes **Done**, which returns to the card with your edit rendered.
  Both modes follow the row selection as you arrow through the table.
  There is no Question / Answer switch any more: with both sides on
  screen there is nothing to flip to. The editor's format toolbar also
  wraps to fit the pane instead of being cut off at its right edge — at
  460px roughly half the buttons, cloze and the settings gear included,
  were unreachable. Switch off with **browse_render_preview**.
- **A trimmed Browse sidebar.** Decks and Tags — the two real ways a
  collection is grouped — and nothing else. Saved Searches, Today, Flags,
  Card State and Note Types are hidden; every one of them was a search
  you can still type (`is:due`, `flag:1`, `added:1`, `note:Basic`).
  Section headings are set as headings, not as rows. Switch off with
  **browse_sidebar_minimal**.
- The Browse editor pane gets the Add window's editor styling (shared
  stylesheet, with pane-specific spacing). Switch off with
  **restyle_browse_editor**.

## [0.3.0] — 2026-09-02
### Fixed
- Sub-decks now open the way you left them. The deck list reads Anki's own
  per-deck collapse flag (the one that syncs), and the chevrons write it
  back — so decks you minimised stay minimised after a restart instead of
  everything expanding on launch. The old behaviour is still available as
  **Sub-decks on startup: Expanded**; there is also **Collapsed**.
- Single-deck home page: a lone top-level deck (e.g. `All::…`) hid all of
  its sub-decks behind the hero card. They now list underneath it, with
  their own chevrons and gears. The hero itself can be switched off.
- Turning the sidebar off left the deck list empty (the tree payload was
  only injected alongside the sidebar).
- Inline rename works again on the redesigned deck rows (it had fallen
  back to Anki's rename prompt after the shared-list refactor).
- The "Show streak counter" setting is wired up; it was a no-op.

### Added
- Move decks without renaming: drag a deck onto another deck to nest it,
  drop it on the top-level zone to un-nest it, or pick **Move to…** from
  the deck's gear menu for a searchable list of destinations. Both drive
  Anki's own reparent operation.
- Settings → **Appearance → Background**: Paper / White / custom colour
  for light mode and Ink / Black / custom for dark mode.
- Settings → **Reviewer → Style card content** — off keeps your note
  type's own CSS (fonts, colours, background) exactly as stock Anki
  renders it; the header and answer keys stay.
- Settings → **Reviewer → Answer buttons** — interval chips (default) or
  Anki's labelled Again / Hard / Good / Easy bar.
- Every opinionated feature now has its own switch, all defaulting to the
  current behaviour: today panel, click-to-reveal, press feedback, inline
  editing, click-a-deck-to-study (vs. Anki's overview page), drag-to-move,
  the command palette, inline Add / Browse / Stats / Preferences windows,
  the redesigned Add window, the redesigned finished-deck page, and quiet
  sync. `config.md` documents each key.
- "Restore Anki Design defaults" now reads the shipped `config.json`, so
  it can never drift from the real defaults.

### Changed
- Default for sub-decks on startup is **Remember** (Anki's synced state)
  rather than always expanded.
- Settings page regrouped into Appearance · Home page · Deck list ·
  Reviewer · Windows · Heatmap · Typography.

## [0.2.0] — 2026-05-24
### Added
- Cmd-K command palette: searches actions, decks, cards, and tags from
  anywhere (deck browser, overview, reviewer). Opens via the sidebar's
  "Search anything…" pill, ⌘K / Ctrl-K, or Cmd-Shift-P.
- Press-feedback animation on ease grading — soft radial bloom expands
  from the pressed key and the interval text fades in place.
- Redesigned congrats page (deck-finished): editorial header, single-
  sentence result, proportional accuracy bar with legend.
- Shared deck-list component: the home tree and the congrats "keep going"
  list render through the same module (`web/decklist.{js,css}`).
- `min_point_version: 250900` in `manifest.json` so older Ankis don't try
  to install a build they can't run.

### Fixed
- Ease-interval chips (`10 min`, `1 day`, etc.) now appear on Image
  Occlusion, Cloze, and any non-FrontSide card types — previously they
  only showed up when the back template included `{{FrontSide}}`.
- Deck-browser single-deck mode no longer renders the deck twice (the
  shared deck-list bails when the hero card owns the page).
- "Search anything…" pill renders with its background and border on the
  congrats page; Anki's Svelte/Bootstrap reset was wiping them.
- "Try custom study" link no longer duplicates on the congrats page when
  there are no other decks with work due.

## [0.1.0]
Initial AnkiWeb release.
### Added
- Base theme injected into the deck browser, overview, and reviewer.
- Contribution-style review-activity heatmap on the deck list, with
  today / streak / total summary.
- Reviewer progress bar with a `done / total` label.
- Themed Add Card, Browse, Stats, Preferences windows.
- Inline rename and a custom deck-options menu on multi-deck rows.
- Inline field editing in the reviewer (in place of the EditCurrent dialog).
- Config: `accent`, `show_heatmap`, `show_progress`, `heatmap_weeks`,
  `theme`, `density`, `sidebar_nav`, `heatmap_palette`,
  `reviewer_card_width`, `reviewer_font_size`, `font_serif`, `font_sans`.
- Declares conflicts with Onigiri, Review Heatmap, and the Progress Bar
  add-on so Anki warns users instead of silently fighting them.

### Known issues
- Anki 25.09's deck-list DOM differs from the classic selectors; some rows
  are low-contrast until the theme selectors are tuned.
- Qt chrome (menus, native dialogs, sidebar) is not themed yet.
