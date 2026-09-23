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
- **backdrop** — what sits behind the deck list and the deck overview.
  `"scene"` (default) draws a layered landscape: sky, stars, moon, drifting
  cloud bank and three mountain ranges, each on its own slow drift. Its
  palette follows the clock, shifting between dawn, day, dusk and night.
  `"aurora"` keeps just the older abstract wash of drifting colour.
  `"video"` plays a looping nature clip from your `user_files/nature/`
  library instead (see **video_selection** below); with no library present
  it quietly falls back to the aurora wash rather than a blank page.
  `"black"` is a flat `#000` background and nothing else — no glow, no
  motion. `"off"` leaves the flat page glow and nothing else. The reviewer
  is never given a backdrop in any mode, deliberately: motion behind a card
  you are trying to recall is a distraction. Nothing is downloaded or
  bundled for the scene backdrop — it's gradients and clip paths in
  `web/scene.css`; the video backdrop plays whatever video files you (or
  a curation tool) put in `user_files/nature/` — whatever format the
  embedded webview can decode, WebM/VP9 on current Anki builds — and
  ships none itself.
- **backdrop_intensity** — `"cinematic"` (default) or `"subtle"`, which
  halves the backdrop's strength without changing what it draws. Ignored
  when **backdrop** is `"off"`. Light themes are damped again on top of
  this, since the scene has to sit under dark text rather than glow out of
  a near-black page.
- **scene**: which landscape the **backdrop** draws when it is `"scene"`.
  `"shuffle"` (default) rotates through all ten, starting on a scene picked
  from the date so the page you open first is not the same one every
  morning, then cycling while the screen stays open. Naming one instead
  (`"peaks"`, `"dunes"`, `"forest"`, `"canyon"`, `"lake"`, `"volcano"`,
  `"isles"`, `"tundra"`, `"spires"`, `"ruins"`) pins it and stops the
  cycling entirely. Anything else falls back to `"shuffle"`. The hour still
  sets the palette on top of whichever scene is up, so each of these looks
  different at dawn, day, dusk and night.
- **scene_shuffle_seconds**: how long each scene holds before the next one,
  `10` by default. Clamped to 2..3600, so a hand-edited `0` cannot spin.
  Ignored when **scene** pins a single scene, when **backdrop** is not
  `"scene"`, and when the system asks for reduced motion, in which case the
  first scene simply stays up.
- **video_selection**: which clip(s) the **backdrop** plays when it is
  `"video"`. `"shuffle"` (default) rotates through the whole
  `user_files/nature/` library. `"biome:<name>"` (e.g. `"biome:ocean"`)
  restricts the rotation to one biome. A bare value naming one of the
  `"file"` entries in `user_files/nature/index.json` (e.g.
  `"ocean/humpback-whale.webm"`) pins that single clip, which then just
  loops on its own — **video_rotate_seconds** has nothing to rotate to.
  An object of the form `{"mode": "custom", "files": ["ocean/x.webm",
  "arctic/y.webm"]}` — what Settings' checkbox list writes — rotates
  among exactly those clips, in library order, regardless of the order
  they're listed here. A biome no longer curated, a file that's been
  deleted, or a custom list whose files are all gone, falls back to
  `"shuffle"` rather than rendering nothing; a custom list with some
  files missing just drops those and keeps the rest.
- **video_rotate_seconds**: how long each clip plays before crossfading to
  the next, `300` (5 minutes) by default. Any positive integer of seconds
  is accepted — Settings' duration field lets you type a value in
  seconds, minutes or hours and stores the total in seconds — but it's
  clamped to 5..86400 (a day) on use; `0` is the one exception — it means
  "never rotate," and plays the first clip's own loop indefinitely. A
  typed value below 5 is not rejected, just rounded up to 5 when the
  backdrop reads it, so a `2` in this file behaves like a `5`. Ignored
  when **video_selection** pins a single file, when **backdrop** is not
  `"video"`, or when only one clip matches the current selection
  (nothing to rotate to either way).
- **backdrop_motion**: which "reduce motion" settings the backdrop obeys.
  `"always"` (the default) obeys your operating system's reduced-motion
  setting only, which is what every other animation in this add-on does.
  `"auto"` additionally obeys Anki's own Preferences > Reduce motion, which
  stops both the animation and the scene cycling and leaves one still
  landscape up. It is opt-in rather than the default because Anki turns that
  preference on unless you have explicitly turned it off, so obeying it by
  default would switch the backdrop off for nearly everybody. Your operating
  system's setting is obeyed either way and is not configurable here: that
  one you chose, where Anki's you probably inherited.
- **font_serif** / **font_sans** — optional display fonts prepended to the
  built-in stacks (e.g. `"Iowan Old Style"`). `""` uses the defaults.

## Home page
- **sidebar_nav** — the left rail replaces Anki's top toolbar. The inline
  windows below need it.
- **sidebar_collapsed** — the left rail's collapsed/expanded state. Toggled
  by the chevron next to the anki+ wordmark (collapsed = icons-only, 64px
  wide; expanded = 264px), not meant to be hand-edited — this just
  remembers what you last left it as, including across restarts.
- **sidebar_width**: the expanded rail's width in px (200 to 480, default
  264). Set by dragging the rail's right edge (double-click the edge to go
  back to 264); like `sidebar_collapsed`, it just remembers what you did.
- **show_today** — the today panel (cards · minutes + per-hour bars).
- **show_streak** — the streak counter above the heatmap.
- **show_heatmap**, **heatmap_weeks**, **heatmap_palette** — the review
  heatmap, its maximum width in weeks (`53` ≈ a year — the grid starts at
  your first review and grows week by week until it hits this cap, then
  rolls forward), and its colour (`"accent"`, `"green"`, `"teal"`,
  `"violet"`, `"rose"`, `"amber"`).
- **hide_bottom_on_decks** / **hide_bottom_on_overview** — hide Anki's
  native bottom strip on those screens.

## Deck list
- **deck_tree_startup** — `"remember"` keeps each deck's own open/closed
  state (Anki's synced flag), `"expanded"` opens everything on launch,
  `"collapsed"` closes every parent on launch.
- **deck_drag_move** — drag a deck onto another to nest it (or onto the
  top-level zone). "Move to…" in the deck menu is always available. Drop
  a deck on the top or bottom **edge** of a sibling instead to resort the
  group: it lands just above or below that deck, so the top edge of the
  first sibling puts it first and the bottom edge of the last puts it
  last. An accent line shows where it will land; the middle of a row still
  nests.
- **deck_order** — the sort order those edge drops produce, keyed by
  parent deck id (`"0"` for the top level) with the child ids in order.
  Anki itself only sorts decks by name, so this lives here rather than in
  the collection. Decks not in a list (new ones, or ones moved in from
  another parent) follow the ranked ones in name order; ids that no longer
  sit under that parent are ignored. Not meant to be hand-edited.
- **deck_subsections** — **New subsection…** on a deck's menu, both on the
  deck list (the gear) and in the Browse sidebar (right-click). Name a
  heading — "Exam 1 Content" — and tick the sub-decks that belong under
  it; they are renamed into it (`Microbiology::Week 1` becomes
  `Microbiology::Exam 1 Content::Week 1`). A rename keeps the deck id, so
  the cards, the scheduling, the preset, the per-day limits and any
  **Memorize by…** deadline come with it — it is the same deck, filed one
  level deeper. Ticking nothing creates the heading empty.
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
- **arrow_key_grading**: grade with the arrow keys. Up = Easy, Right =
  Good, Left = Hard, Down = Again. On the question side any arrow shows the
  answer (like Space) without grading. Ignored while you type in a field or
  the quick editor, or while the command palette is open. `false` gives the
  arrows back to scrolling.
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
  this deck's limit is 20" — and offers to raise the limit in one click;
  it confirms with "Limit raised to 36/day ✓". The raise is written as
  *this deck's* new-cards/day limit (Anki's Daily limits → This deck),
  never the preset's, so it can't drag along every other deck sharing the
  preset. Limits you set yourself in deck options are left alone too.
  `false` removes the menu item and stops enforcing any deadline already
  set.

  A date in the **past** is allowed, and is how you record that a deck was
  learned on time — the gear row then reads "Memorized by Sep 8 —
  passed ✓". The interval cap comes off, and the dialog asks what the deck
  should do from there; the choice is remembered per deck:
  - **Maintain long-term** — reviews carry on as normal. Cards
    coming due after the deadline are memory upkeep, not a missed target;
    that is how spaced repetition holds something you already know.
  - **Pause reviews** (default, see `deadline_passed_mode`) — the deck
    stops presenting anything, by setting
    *that deck's own* per-day review and new limits to 0 (not its
    preset's, which other decks share). Reversible, and no card is
    suspended or otherwise altered: switch back to Maintain, or clear the
    deadline, to resume — and whatever limits the deck had before the
    pause, including one you raised to hit the deadline, come back with
    it.
- **deadline_passed_mode** — what a deck does once its deadline is behind
  it, for every deck that hasn't been asked. `"pause"` (the default) stops
  the deck presenting anything; `"maintain"` keeps reviews coming. A deck
  you *have* answered for in the dialog keeps its own answer either way —
  this key only fills in the blank, and it fills it in live, so changing
  it moves every un-answered deck at once without rewriting anything. It
  is also what the dialog pre-selects when a deck has made no choice yet.

  **This diverges from how the feature originally shipped**, where the
  default was `"maintain"` and the dialog wrote that into every deck as
  though it had been chosen. The reasoning for the change: a deadline that
  keeps presenting cards after the date is not a deadline, it is a label.
  Auto-pause is the point of setting one — six decks due "by today" still
  offering 385 reviews the evening the date passed is the failure that
  prompted it. Set this to `"maintain"` to get the old behavior back for
  every deck that hasn't chosen otherwise.

  Deadlines that had *already* passed under the old default are moved onto
  the new one once, on the first launch after this change, so the fix
  reaches decks that went quiet before it shipped. That one-time pass only
  touches entries that never recorded a choice of their own; anything you
  pick afterwards is yours and is never revisited.

## Windows
- **cmdk** — the ⌘K / Ctrl+K command palette.
- **bug_report_shortcut** — Qt key sequence for "Report a bug" (default
  `"Ctrl+Shift+B"`, which Qt maps to ⌘⇧B on macOS). Always also reachable
  from the command palette ("Report a bug") on every screen. Saves a
  ticket — a one-line note, an optional "what did you expect", and a
  screenshot by default — under `~/AnkiTickets/<id>/`.
- **embed_add**, **embed_browse**, **embed_stats**, **embed_settings** — open
  those inside the main window (needs `sidebar_nav`); `false` uses Anki's
  separate windows.
- **restyle_addcard** — the redesigned Add window. `false` restores Anki's
  stock Add window.
- **congrats_redesign** — the redesigned finished-deck page.
- **silent_sync** — sync progress in the sidebar instead of a dialog.
- **fullscreen_bar_inset**: in macOS full screen, the hidden "Anki+" title
  bar reveals the way Safari's does: when the cursor touches the top of the
  screen the bar slides down and the app slides down with it, in the same
  animation, and both slide back when the cursor leaves. AppKit drives it:
  while the window is full screen it carries an empty toolbar that hides
  with the menu bar, which is what makes macOS move the content with the
  bar. Nothing is reserved or painted, and the windowed title bar is
  unchanged. `false` leaves stock behaviour, where the bar slides over the
  top of the page.

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
- **browse_sidebar_multiselect** — the sidebar picks several decks without
  a mode. Anki puts multi-select behind a two-button tool row above the
  tree (Search / Select); with this on, the tool row is gone and the tree
  is always in the Select tool's mode. A plain click still searches the
  deck you clicked. ⌘-click (Ctrl-click) adds a deck to the selection and
  ⇧-click extends a run, without searching. Dragging any selected deck
  onto another moves the whole selection in one undoable step, and
  dragging from the empty space under the last deck lassos a rubber-band
  across rows. Right-click acts on everything selected: **Move to…**
  lists every deck the selection could go into (plus *Top level*), and
  **New subsection…** arrives with those decks already ticked, filed
  under the deepest deck they share. `false` restores Anki's tool row and
  its single-selection default.
- **browse_card_drag** — drag cards out of the Browse table. Press on a
  selected row and pull: every selected card comes along (in Notes mode,
  every card of every selected note). Press on an unselected row and pull
  sideways to drag just that card; pull up or down the list instead and
  the table selects the rows you sweep across, as it always has, so a
  group can be gathered by hand and then dragged. Drop on a deck in the sidebar to
  move them there, one undoable step, the same op as **Change Deck**;
  the list re-runs its search so the moved cards leave it.
  Drop on another row of the table to reposition new cards just before
  that row's card in the new-card queue (the row has to be a new card
  too, since only new cards have a position; a drop on a review card
  says so and changes nothing). Filtered decks, tags and
  headings refuse the drop. A plain click on a selected row still
  collapses the selection to it. `false` leaves the table as Anki ships
  it, with **Change Deck** (⌘D) and **Reposition** as the only routes.
- **clean_deck_names** — decks go by their title. Click a deck in the
  Browse sidebar and the search box reads *Amino Acids*, not
  `"deck:Fundamentals of Biochemistry::Amino Acids"`; the Deck column
  shows *Amino Acids* too, with the full path as the row's tooltip. The
  real query is still what runs, and still what Enter, ⌘-click (AND),
  ⇧-click (OR), **Create Filtered Deck** and **Save Current Search** see;
  the moment you type in the box, what you typed is the search. The
  reviewer header, the Stats title and the Add screen's deck picker use
  the title as well. `false` shows Anki's paths and queries everywhere.
- **browse_render_preview** — the Browse right-hand pane leads with the
  selected card, drawn exactly as the reviewer draws it (note type CSS,
  cloze, images, and your Reviewer settings), front *and* back in one
  view, with the note's tags in a strip underneath. The fields editor is
  a mode you switch into with the pencil in the pane's header (⌘E /
  Ctrl+E); the pencil becomes **Done** and takes you back to the card,
  which re-renders with whatever you changed. Both modes follow the row
  selection live. `false` gives the fields the whole pane back, as in
  stock Anki. Needs `embed_browse`: the pane is built when Browse opens
  as a tab. With Browse in its own window you still have Anki's own
  preview on `Ctrl+Shift+P`.
- **restyle_browse_editor** — apply the Add window's editor styling to
  the Browse fields pane. `false` keeps Anki's stock editor there.
- **browse_tree_collapsed** / **browse_table_collapsed** — whether the
  Browse sidebar tree (Decks/Tags) and the card-list table are currently
  hidden. Toggled by the **Sidebar** / **Table** buttons in the small
  toolbar above the panes — hiding either one hands its width to the
  editor/preview pane, useful while adding or editing a card. Both
  toggles stay reachable in that same toolbar no matter what else is
  collapsed. Not meant to be hand-edited; remembered across restarts.

## Images in fields

Not a setting — a note on where the controls are, because both editors
(the Add tab and the Browse pane's pencil mode) use Anki's own machinery,
and it is worth knowing which knob belongs to whom.

Three ways to get a picture into a field, all Anki's: the **paperclip**
in the format toolbar (or `F3`) opens a file picker, **paste** an image
from the clipboard, and **drag a file** onto a field. Each copies the
file into `collection.media` and inserts an `<img>` at the cursor.

To **resize**, click an image in a field. A dashed frame appears with
corner handles and a small toolbar beneath it (float left / none / right,
actual size, restore original). Out of the box the handles are inert and
the frame reads *"(double-click to expand)"* — that is Anki's **Shrink
Images** option, on by default, which previews large images at a reduced
size. Double-click the image (or press the toolbar's actual-size button)
to release it, then drag any corner; the label tracks the live size.
Turn Shrink Images off in the toolbar's **gear** menu to have the handles
live on the first click. That checkbox is Anki's own, stored with the
collection, and it applies to every editor.

Dragging writes a plain `width=` onto the `<img>`, so the size travels
with the note and shows up on the card. The card in review and the Browse
read pane both cap media at the width of the content column
(`max-width: 100%`, with `height: auto` keeping the aspect ratio): a width
**smaller** than the column is honoured exactly, a width **larger** than
it is capped down to the column. So resizing is reliable for making an
image smaller, and past the column width it simply has no further effect.

Changes take effect the next time the relevant screen is drawn (return to
the deck list / start a review). The four **Browse** options above are
read when the Browse tab opens, so close and reopen it after changing
them. Restarting Anki is never required.
