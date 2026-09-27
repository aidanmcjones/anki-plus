"""Live test: hours of reviewing must not grow the reviewer page.

Ticket 20260923-153047: after about 2.5 hours in the Enzymes deck, the next
card turned the whole window grey. The main webview's renderer had died of
an out-of-memory (V8/Oilpan) while the app process itself stayed idle.

This opens the real reviewer on a 40-card fixture deck whose note type
carries a FunBiochem-like stylesheet (several px font sizes, sub/sup, a
table, a slide-sized image on the answer), then answers through at least
RF_MEM_VIEWS (default 300) card views the way the user does: show the
answer, rate Good, repeat (the deck's learning steps bring the 40 cards
round again and again, so it never runs out and the page is never
reloaded; should it run out anyway, the deck is reset and reopened). After
10 views and every 50 views it reads from the main webview: the total text
of every <style> in the document, the CSSOM rule count, the DOM element
count, and performance.memory.usedJSHeapSize; it also samples the resident
memory of the page's renderer process (reported, not asserted: without a
forced GC it swings with V8's schedule). The page must stay flat: style text
and rule count after the last view within 5 percent of those after 10
views, the JS heap growing no more than 20 MB.

With the Exam 1 FunBiochem deck on disk (RF_MEM_APKG, default the course
folder's Exam1_FunBiochem.apkg) the note type CSS is read from it instead
of the embedded copy, so the deck the ticket was filed on is what runs.
"""

import base64
import json
import os
import sqlite3
import subprocess
import tempfile
import zipfile

VIEWS = int(os.environ.get("RF_MEM_VIEWS", "300"))
N_CARDS = 40
APKG = os.environ.get("RF_MEM_APKG") or os.path.expanduser(
    "~/Library/CloudStorage/OneDrive-TexasChristianUniversity/"
    "Fundamentals of Biochemistry/Anki/Exam1_FunBiochem.apkg")

# FunBiochem-E1's note type CSS, trimmed: px sizes on .card, .answer,
# .scenario, table, .src, sub/sup inside the cloze placeholder.
FIXTURE_CSS = """
.card { font-family: -apple-system, sans-serif; font-size: 17px; text-align: left; padding: 6px 10px; }
img { max-width: 100%; height: auto; background: #fff; border-radius: 4px; }
.imgs img { max-height: 38vh; width: auto; object-fit: contain; }
.scenario { color: #555; padding: 5px 9px; margin-bottom: 6px; font-size: 13px; line-height: 1.35; }
.answer { font-size: 17px; line-height: 1.4; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { border: 1px solid #8886; padding: 3px 6px; }
table.seq td { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
.src { color: #888; font-size: 11pt; margin-top: 8px; }
summary { color: #777; font-size: 0.8rem; }
.cloze { color: #6c8cff; font-weight: bold; }
.fb-front .cloze-inactive { font-size: 0; }
.fb-front .cloze-inactive::after { content: "[...]"; font-size: 17px; color: #888; }
.fb-front .cloze-inactive sub, .fb-front .cloze-inactive sup { font-size: 0; }
"""

QFMT = ('<div class="scenario">Scenario {{Id}}</div>{{Question}}'
        '<div class="imgs qimgs"></div>')
AFMT = ('{{FrontSide}}<hr id=answer><div class=answer>{{Answer}}</div>'
        '<div class="imgs">{{AnswerImage}}</div><div class=src>{{Id}}</div>')

METRICS_JS = r"""(function () {
  var len = 0, n = 0;
  document.querySelectorAll('style').forEach(function (s) { len += (s.textContent || '').length; n++; });
  var rules = 0, cssom = 0;
  function walk(list) {
    for (var i = 0; i < list.length; i++) {
      rules++; cssom += (list[i].cssText || '').length;
      var inner = null; try { inner = list[i].cssRules; } catch (_) {}
      if (inner && inner.length) walk(inner);
    }
  }
  for (var i = 0; i < document.styleSheets.length; i++) {
    try { walk(document.styleSheets[i].cssRules); } catch (_) {}
  }
  var m = window.performance && performance.memory;
  var ans = null;
  document.querySelectorAll('#qa style').forEach(function (s) {
    try { for (var j = 0; j < s.sheet.cssRules.length; j++) {
      var r = s.sheet.cssRules[j]; if (r.selectorText === '.answer') ans = r.style.fontSize; } } catch (_) {}
  });
  return { styleText: len, styleEls: n, sheets: document.styleSheets.length, rules: rules,
           cssomText: cssom, elements: document.getElementsByTagName('*').length,
           heap: m ? m.usedJSHeapSize : null, answerFontSize: ans };
})()"""


DIAG_JS = ("(function(){var q=document.getElementById('qa');return q?{mark:!!document.getElementById('rm-mark'),"
           "opacity:q.style.opacity,n:q.childNodes.length,html:q.innerHTML.slice(0,120)}:'no #qa';})()")


def _deck_css():
    """The Exam 1 FunBiochem note type CSS when the deck is on disk."""
    if not os.path.exists(APKG):
        return None, "embedded fixture CSS"
    try:
        with zipfile.ZipFile(APKG) as z, tempfile.TemporaryDirectory() as d:
            name = "collection.anki21" if "collection.anki21" in z.namelist() else "collection.anki2"
            z.extract(name, d)
            con = sqlite3.connect(os.path.join(d, name))
            try:
                models = json.loads(con.execute("select models from col").fetchone()[0])
            finally:
                con.close()
        for m in models.values():
            if m.get("name") == "FunBiochem-E1" and m.get("css"):
                return m["css"], f"FunBiochem-E1 CSS from {os.path.basename(APKG)}"
    except Exception as e:  # unreadable deck: fall back to the fixture
        return None, f"embedded fixture CSS ({e!r} reading the deck)"
    return None, "embedded fixture CSS (no FunBiochem-E1 note type in the deck)"


def _slide_png(path):
    """A slide-sized (2001x1125) PNG like the Enzymes deck's images."""
    from aqt.qt import QColor, QImage, QPainter

    img = QImage(2001, 1125, QImage.Format.Format_RGB32)
    img.fill(QColor("#f4f1ea"))
    p = QPainter(img)
    for i in range(0, 2001, 40):
        p.fillRect(i, (i * 7) % 1125, 30, 90, QColor((i * 5) % 255, 120, 200))
    p.end()
    img.save(path, "PNG")


def _make_deck(t, css):
    col = t.mw.col
    mm = col.models
    m = mm.new("RenderMemory")
    for fname in ("Id", "Question", "Answer", "AnswerImage"):
        mm.add_field(m, mm.new_field(fname))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"], tmpl["afmt"] = QFMT, AFMT
    mm.add_template(m, tmpl)
    m["css"] = css
    mm.add(m)
    m = mm.by_name("RenderMemory")
    did = col.decks.id("RenderMemory")
    conf = col.decks.config_dict_for_deck_id(did)
    conf["new"]["perDay"] = 9999
    # Twelve one-minute learning steps: Good moves a new card one step, and
    # every step stays inside the 20-minute learn-ahead, so the 40 cards
    # come round 12 times (480 views) before any graduates. The deck never
    # runs out inside 300 views, so the reviewer page is never reloaded and
    # the whole count happens in one page, the way a long session does.
    conf["new"]["delays"] = [1.0] * 12
    conf["rev"]["perDay"] = 9999
    col.decks.update_config(conf)
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "rm_slide.png")
        _slide_png(src)
        media = col.media.add_file(src)
    for i in range(N_CARDS):
        note = col.new_note(m)
        note["Id"] = f"rm-{i:02d}"
        note["Question"] = (f"Card {i}: what is K<sub>m</sub> when V<sub>0</sub> = "
                            f"&frac12; V<sup>max</sup>?")
        note["Answer"] = (f"<b>[S] = K<sub>m</sub></b> ({i})<table class=seq><tr>"
                          "<th>[S]</th><th>V<sub>0</sub></th></tr><tr><td>K<sub>m</sub>"
                          "</td><td>V<sup>max</sup>/2</td></tr></table>")
        note["AnswerImage"] = f'<img src="{media}">'
        col.add_note(note, did)
    col.decks.select(did)
    return did


def _renderer_rss_mb(t):
    try:
        pid = int(t.mw.web.page().renderProcessPid())
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return round(int(out) / 1024.0, 1) if out else None
    except Exception:
        return None


def run(t):
    mw = t.mw
    css, css_src = _deck_css()
    t.note(f"note type CSS: {css_src}")
    did = _make_deck(t, css or FIXTURE_CSS)
    rv = mw.reviewer

    def start():
        mw.col.sched.schedule_cards_as_new(
            mw.col.find_cards(f"did:{did}"), restore_position=True, reset_counts=True)
        mw.col.decks.select(did)
        mw.moveToState("review")
        return t.wait_until(lambda: mw.state == "review" and rv.card is not None, timeout=20)

    t.check("reviewer opened on the fixture deck", start(), mw.state)

    def mark():
        # A marker in #qa; Anki's _updateQA replaces #qa's contents, so the
        # next card side is on screen once the marker is gone and #qa is
        # visible again.
        t.js("(function(){var q=document.getElementById('qa');if(!q)return 0;"
             "var i=document.createElement('i');i.id='rm-mark';q.appendChild(i);return 1;})()")

    def shown(state):
        if mw.state != "review":
            return True  # deck finished: the loop restarts it
        if rv.state != state or rv.card is None:
            return False
        return t.js("(function(){var q=document.getElementById('qa');"
                    "return !!q && !document.getElementById('rm-mark') && "
                    "q.style.opacity !== '0' && q.childNodes.length > 0;})()", timeout=5)

    samples = {}
    views = 0
    wraps = 0
    stuck = None
    while views < VIEWS:
        if mw.state != "review" or rv.card is None:
            wraps += 1
            if not start():
                stuck = f"could not restart the deck after {views} views ({mw.state})"
                break
        if not t.wait_until(lambda: shown("question"), timeout=10, step_ms=20):
            stuck = (f"question {views + 1} never rendered (state {mw.state}/{rv.state}, "
                     f"card {rv.card and rv.card.id}, page {t.js(DIAG_JS, timeout=5)})")
            break
        if mw.state != "review":
            continue
        views += 1
        if views == 1 or views == 10 or views % 50 == 0:
            m = t.js(METRICS_JS, timeout=10)
            m["rendererRssMB"] = _renderer_rss_mb(t)
            samples[views] = m
            t.note(f"after {views} views: {json.dumps(m)}")
        mark()
        rv._showAnswer()
        if not t.wait_until(lambda: shown("answer"), timeout=10, step_ms=20):
            stuck = (f"answer {views} never rendered (state {mw.state}/{rv.state}, "
                     f"page {t.js(DIAG_JS, timeout=5)})")
            break
        mark()
        rv._answerCard(3)
    t.note(f"{views} card views, {wraps} wrap(s) of the {N_CARDS}-card deck")
    t.check(f"answered through {VIEWS} card views", views >= VIEWS and not stuck, stuck or views)
    first, last = samples.get(10), samples.get(max(samples) if samples else 0)
    if not first or not last or last is first:
        t.check("samples taken after 10 and the last view", False, samples)
        return

    def within(key, pct):
        a, b = first[key], last[key]
        return a and abs(b - a) <= a * pct / 100.0, f"{key}: {a} after 10 views, {b} after {max(samples)}"

    ok, detail = within("styleText", 5)
    t.check("total <style> text stays within 5% of the 10-view size", ok, detail)
    ok, detail = within("rules", 5)
    t.check("CSSOM rule count stays within 5% of the 10-view count", ok, detail)
    ok, detail = within("elements", 5)
    t.check("DOM element count stays within 5% of the 10-view count", ok, detail)
    t.check("the answer's font-size is rewritten once, not nested",
            (last.get("answerFontSize") or "").count("calc(") <= 1, last.get("answerFontSize"))
    if first.get("heap") is not None and last.get("heap") is not None:
        grow = (last["heap"] - first["heap"]) / 1e6
        t.check("JS heap grows no more than 20 MB",
                grow <= 20, f"{first['heap']} -> {last['heap']} bytes ({grow:+.1f} MB)")
    else:
        t.note("performance.memory unavailable; heap check skipped")
