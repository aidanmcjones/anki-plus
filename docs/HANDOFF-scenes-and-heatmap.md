# Handoff: living scene backdrops + heatmap weekday labels

You are picking this up cold. Everything you need is here; nothing depends on a
prior conversation. Two independent work items, A (bigger) and B (smaller).
Do both. Read the whole document before touching code.

Repo to work in: `~/dev/anki-design`. Consider copying this file to
`docs/HANDOFF-scenes-and-heatmap.md` in that repo as part of your first commit.

---

## 0. What this repo is, and the one thing that will confuse you

This is the **Anki Design add-on**, which themes Aidan's personal Anki build
("Anki+"). It is a *different repo* from the Anki fork:

| Thing | Path | Remote (`origin`) |
| --- | --- | --- |
| The theme add-on (you work here) | `~/dev/anki-design` | private `anki-design-private` |
| The Anki app fork (do not touch) | `~/dev/anki` | private `anki-private` |

Upstream for this repo is `NoahLloyd/anki-design` (MIT). Aidan's fork rebranded
the UI to "Anki+", so ignore any "anki.design" branding you find.

**The confusing part: this checkout is consumed two different ways.**

1. **Aidan's real, running app.** His Anki profile has a symlink in
   `addons21` pointing straight at `~/dev/anki-design`, so edits here reach his
   live app on the next app restart, with no build step.
2. **A dev sandbox.** `make dev` / `make demo` rsync the tree to
   `~/Library/Caches/anki-design-sandbox/<name>` and run a *separate* Anki
   against a demo collection. **Edits in the checkout do not reach that
   sandbox until you run `make sync`.**

Use the sandbox for anything visual. Do not drive his live app: it holds his
real collection (Biochemistry and Microbiology decks he is actively studying,
several hundred reviews due). See section 4; the rules there are strict and
exist for good reasons.

Restarting his live app is how a change goes live: `pkill -TERM -f
"tools/run.py"`, then `open "/Applications/Anki+.app"`. It interrupts whatever
he is doing, so say so first, and never do it mid-review without asking.

---

## 1. Work item A: dynamic scene backdrops

### The ask, in Aidan's words

He wants "scenes like these" (a fantasy-landscape imgur album) built into
"dynamic moving backgrounds for the deck menu, behind the progress and deck
menu dropdown UI".

### The reference is unreachable. Read this before planning the art.

The album (`https://imgur.com/a/fantasy-landscapes-SzNBx`) **cannot be
fetched** from this environment; imgur is blocked at the tool layer, confirmed
by the previous agent. Therefore:

- **Do not** try to download it, and do not bundle third-party art you cannot
  license. See the asset promise in 1.4.
- **Default (recommended): build original scenes procedurally in code**, in
  the spirit of the genre: layered mountain ranges receding into atmospheric
  haze, an oversized moon or low sun near the horizon, a silhouetted foreground
  ridge, drifting cloud banks, a star field or aurora ribbon, deep saturated
  skies (indigo, violet, teal), painterly gradients rather than hard edges.
- If literal fidelity matters, **ask Aidan to drop image files into
  `web/scenes/`** rather than sourcing them yourself, and state the trade-off:
  repo size and redistribution rights.

He was offered a clarification round on art source, rotation and intensity and
declined it, so the defaults below were chosen for him. They are yours to
sanity-check, not to silently reverse.

### 1.1 What exists today (build on it, do not fight it)

An animated abstract "aurora" backdrop already ships (commit `0f7916c`), at the
**end of `web/theme.css`, lines 1112-1205**:

- Keyframes `ba-aurora-a` / `ba-aurora-b` (`:1122-1131`) animate **only**
  `transform`, keeping motion on the compositor.
- Far layer `body:has(.ba-home)::before, body:has(.ba-over)::before`
  (`:1143-1159`): `inset: -25%`, `var(--rf-glow)` plus two radial gradients
  using `color-mix(in oklab, var(--rf-accent) ...)`, `filter: blur(60px)`,
  64s drift.
- Near layer `...::after` (`:1162-1186`): four gradients where the **first is
  deliberately a `--rf-paper` scrim stacked over the blooms** to protect text
  contrast; `blur(70px) saturate(115%)`, 44s drift, `opacity: .88`.
- Light-mode damping (`:1190-1199`), reduced-motion guard (`:1202-1205`).
- Scoped to `body:has(.ba-home)` and `body:has(.ba-over)`: **deck browser and
  overview only. The reviewer is excluded on purpose**, because motion behind a
  card you are trying to recall is a distraction. Keep that boundary.

### 1.2 Three traps that have already bitten someone

1. **`body { isolation: isolate }` at `theme.css:1139` is load-bearing.** The
   layers sit at `z-index: -1`. Without a stacking context on `body` they join
   the *root* stacking context and body's own opaque background paints over
   them. This is why the theme's original `--rf-glow` was invisible for its
   entire life before `0f7916c`. If your backdrop renders as flat colour, check
   this first.
2. **The far layer inherits four declarations from an older rule.** `content`,
   `position: fixed`, `pointer-events` and `z-index` come from the pre-existing
   `body::before` at `theme.css:189-196`; they are *not* repeated in the aurora
   block. Refactor either rule carelessly and the backdrop silently vanishes.
3. **`.ba-home` is applied by string-replacing `<center>`** in `__init__.py`
   (`:361-363`; `.ba-over` at `:369-371`). If you add wrapper markup, do not
   break that substitution.

### 1.3 Recommended architecture

- **Layered scene, transform-only motion.** Sky gradient, far range, mid range,
  foreground silhouette, weather/particles: each its own element or
  pseudo-element, each drifting at a different rate. Parallax comes from
  differing translate distances, not from JS.
- **Compute time of day in Python, not JavaScript.** The head-injection block
  already emits `data-rf-theme`, `data-rf-density` and `--rf-accent`
  (`__init__.py:287-314`), and the deck browser re-renders on every state
  change. Emit something like `data-rf-sky="dawn|day|dusk|night"` there and let
  CSS swap palettes off the attribute.
  This matters: the repo has **no** `visibilitychange` or
  `IntersectionObserver` pattern anywhere, and all twelve
  `requestAnimationFrame` call sites are one-shot deferrals, never loops. A
  sustained JS loop would be the first thing here needing a lifecycle guard
  that does not exist, and would burn CPU while hidden. Pure CSS sidesteps it:
  the compositor stops when the webview stops painting.
- If you are convinced canvas is required, you must add a visibility guard and
  teardown on page unmount, and explain why CSS was insufficient.
- **Keep a scrim.** Deck rows, the hour chart, the streak and the heatmap sit on
  top of this. The existing `--rf-paper` scrim gradient is the proven
  technique. Verify contrast after your change, not before.

### 1.4 Asset and config rules

- `README.md:19` promises: "No network calls, no bundled binaries - keeps
  AnkiWeb review trivial." Honour it; nothing may be fetched at runtime.
- Art must be inline or a local file under `web/`. Precedents: relative `url()`
  font refs (`web/logo.css:12-18`) and inline `data:image/svg+xml` masks
  (`web/reviewer-bottom.css:191-202`). Today `web/` holds only four `.svg`
  files and two `.woff2` fonts, **no raster art at all**, so prefer vector/CSS.
- Assets are served host-relative from `/_addons/<dir>/web/...` (`WEB` at
  `__init__.py:66-67`, `setWebExports` at `:70`), so relative URLs inside CSS
  resolve correctly.
- **Add a config toggle.** The aurora currently ships unconditionally with no
  off switch. Add at least an on/off and an intensity (or scene) key to
  `config.json`, document it in `config.md`, and follow the existing appearance
  keys (`theme`, `accent`, `background_light`, `background_dark`, `density`,
  `heatmap_*`).
- `background_light` / `background_dark` are **live**, not dead config:
  `_background_rules` (`__init__.py:89-125`) calls `colors.background_override`
  (`colors.py:63-70`), overriding `--rf-paper` and friends with `!important`.
  Your scrim reads `--rf-paper`, so a custom paper must not wreck legibility.

---

## 2. Work item B: heatmap weekday labels and packing

Smaller, well understood, root cause already diagnosed. Two changes.

### 2.1 Letters for every weekday M-F

Only Mon/Wed/Fri are labelled today. In `__init__.py:826-829`:

```python
weekdays_html = "".join(
    f'<span class="rf-hm-wd">{_WEEKDAYS[i][0] if i in (1, 3, 5) else ""}</span>'
    for i in range(7)
)
```

All seven spans are always emitted (`_WEEKDAYS` at `:642`, Sunday-first); only
indices 1/3/5 get text. Widen to Mon-Fri.

**Gotcha:** `_WEEKDAYS[i][0]` yields `"T"` for *both* Tuesday and Thursday. Use
an explicit glyph table rather than slicing. If you choose two-character labels
(`Tu` / `Th`), the gutter's `margin-right: 4px` (`web/heatmap.css:32`) is sized
for single letters and must widen.

Aidan asked for "all m-f". Chosen default: **keep all 7 rows** so weekend study
still shows, but label only Mon-Fri; weekend rows keep empty spacers.

### 2.2 Pack the squares against the letters

**Symptom:** the letter column sits far left while the (now narrow) grid floats
centred, so they read as two unrelated objects.

**Root cause:** `.rf-hm-wds` (the letter gutter) is a **sibling outside**
`.rf-hm-scroll`, while `.rf-hm-inner { width: fit-content; margin: 0 auto }`
(`web/heatmap.css:81-84`, added in `ce5d32e`) centres only the grid inside the
full-width scroll viewport. Current DOM, from the template at
`__init__.py:839-862`:

```
.rf-heatmap
  .rf-hm-head   > .rf-hm-title, .rf-hm-stats
  .rf-hm-body                       <- flex row
    .rf-hm-wds  > .rf-hm-mon-spacer, 7 x .rf-hm-wd     <- OUTSIDE the scroller
    .rf-hm-scroll                                      <- overflow-x: auto
      .rf-hm-inner                                     <- margin: 0 auto
        .rf-hm-months > .rf-hm-mon
        .rf-hm-grid   > .rf-hm-col > .rf-hm-cell x7
```

**Preferred fix:** move the gutter *inside* `.rf-hm-inner`, making it a flex row
of (letters, then a column of months + grid), so letters and squares centre as
one unit. Keep the letters `position: sticky; left: 0` so they stay put once
history grows long enough to scroll horizontally.

**Rejected alternative:** simply setting `margin: 0` on `.rf-hm-inner`. That
reverts the deliberate centring added in `ce5d32e`, which stopped a short
history from hugging the left edge.

### 2.3 Metrics and things you must not regress

- Cell geometry is hard-coded as `11px` / `3px` in `web/heatmap.css`
  (`.rf-hm-col` `:95-102`, `.rf-hm-cell` `:131-138`, `.rf-hm-wd` `:37-44`) and
  again as `span * 14` in `__init__.py:820` (14 = 11 + 3). **No CSS custom
  properties exist for these**, so any resize means touching all of them.
- Letter-to-row alignment is maintained by hand-matching `11px + 3px` in the
  gutter against the same numbers in the cells, plus
  `.rf-hm-mon-spacer { height: 20px }` (`:36`) offsetting the months row.
- Do not break: the new-column growth animation (`heatmap.css:105-129`,
  `web/heatmap.js:103-131`, module-scope `lastColCount` at `:19`), the
  month-label suppression rule (`__init__.py:818-823`), the rolling window cap,
  or `_heatmap_window()` (`__init__.py:714-752`).
- `tests/test_heatmap_window.py` has five tests (short history, the 53-week cap,
  zero reviews, one-column-per-week growth, the ceiling). They must stay green.

---

## 3. Recent related work, for continuity

- `ce5d32e` made the heatmap grid **grow with actual history** instead of always
  drawing a fixed year. Previously `min(earliest_review, window_floor)` meant
  the grid was never smaller than the cap, so 10 days of history drew 53
  mostly-empty columns. It now renders 4.
- `0f7916c` added the aurora you are extending, plus the `isolation` fix in 1.2.

Aidan is a student using this daily; the deck screen is the first thing he sees
each session.

---

## 4. Verification protocol. Not boilerplate; read it.

### 4.1 Use the repo's silent capture channel, in the sandbox

This repo ships a dev-only side channel precisely so agents can drive and
screenshot Anki **without the window surfacing or stealing focus**. Full docs in
`CLAUDE.md` at the repo root. Summary:

```bash
make dev && make demo          # sandbox + demo collection
make sync                      # REQUIRED after editing the checkout
echo "decks" >> .context/cmd   # drive state
scripts/snap.sh out/decks.png main                          # silent screenshot
scripts/snap.sh out/x.png main --width=1200 --height=1010   # warm first, then re-shoot
scripts/dump.sh out/decks.html main                         # DOM dump
```

Time-wasters if ignored: the window must exist and not be minimized (WebEngine
suspends paints when iconified); resize and grab must be **two separate calls**
with a sleep, or the new area renders black; Python edits need an Anki restart
(`make demo-stop && make demo`), while `web/` edits hot-reload after `make sync`.

### 4.2 Do not use `screencapture` or `osascript`

`CLAUDE.md` says so explicitly, and experience backs it: a previous agent's
`screencapture` grabbed the terminal once and a browser window another time,
because it captures whatever is frontmost, not the app. If a state is
unreachable through `.context/cmd`, **add a command to `_dev_run_cmd` in
`__init__.py`** rather than reaching for focus-stealing tools.

### 4.3 Checking Aidan's live app (rarely, carefully)

The silent channel is deliberately refused in his real profile
(`_dev_profile_allowed` requires an `Anki2-dev/` base, an `anki-design-demo*`
profile, or `ANKI_DESIGN_DEV=1`) so scripted probes can never drive his real
collection. Do not disable that gate.

If you must confirm something live, query the remote debugging port read-only.
This is how the aurora and the 4-column heatmap were confirmed:

```python
# ~/dev/anki/out/pyenv/bin/python  (it has the `websocket` module)
import json, urllib.request, websocket
t = [x for x in json.load(urllib.request.urlopen("http://localhost:8080/json"))
     if x.get("title") == "main webview"][0]
ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=15)
ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
    "expression": "getComputedStyle(document.body,'::after').backgroundImage.slice(0,80)",
    "returnByValue": True}}))
while True:
    m = json.loads(ws.recv())
    if m.get("id") == 1:
        print(m["result"]["result"]["value"]); break
```

Confirm a file actually reached the browser with
`curl -s http://localhost:40000/_addons/anki-design/web/theme.css | grep ...`.

### 4.4 Required checks before you commit

- `node --check` on every `.js` you touch.
- `python3 -m py_compile __init__.py`.
- `python3 tests/test_wrap_fields.py`, `python3 tests/test_study_state_dispatch.py`,
  `python3 tests/test_heatmap_window.py` (all standalone; no pytest, no `make test`).
- Any new geometry, palette or time-of-day math gets a **pure-logic test** in the
  same stub style as `tests/test_heatmap_window.py` (stubs `aqt`, loads
  `__init__.py` via `importlib`).
- Confirm the reduced-motion path: under `prefers-reduced-motion: reduce` the
  scene must be still but still visible.

---

## 5. Ground rules

- Commit to this repo (`origin` = private `anki-design-private`). **Stage only
  the paths you touched**; other agents sometimes work here concurrently, so
  never `git add -A`.
- End commit messages with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Do not push unless asked; Aidan or the coordinating session handles that.
- Comments explain *why*, not *what*. Read neighbouring code first and match its
  voice; this codebase has a strong, dry house style.
- Never use em dashes in anything Aidan will read.
- Work item B is self-contained and low-risk. If you run long, land B first so
  something is shippable, then continue on A.

## 6. Decisions taken for you (say so if you disagree)

| Decision | Chosen | Why |
| --- | --- | --- |
| Art source | Procedural, in code | Reference unfetchable; no licence to redistribute third-party art; keeps the add-on small |
| Motion | CSS transform layers, no JS loop | No visibility-guard pattern exists here; the compositor handles pausing |
| Time of day | Computed in Python, exposed as a data attribute | Matches the existing head-injection pattern; avoids a timer |
| Intensity | Cinematic with a scrim under content | Proven by the aurora; legibility is non-negotiable |
| Weekday rows | 7 rows kept, letters Mon-Fri only | Matches "all m-f" without discarding weekend study data |
