"""Live: the left rail (Anki+ sidebar) can be widened by dragging its right
edge with the real mouse.

What the user does: hover the rail's right border, a resize handle shows
(col-resize cursor), press and drag it to the right. What they must see:
the rail grows while the pointer moves (not only on release), the home
page content shifts right by the same amount, the new width survives a
re-render of the deck list (it is persisted in the add-on config), and
the Python side that positions Browse/Add/Stats/Settings overlays reports
the same width.

On the unfixed code the rail has no handle and a press-and-drag on its
edge changes nothing, so every check here fails.
"""

import sys

ADDON = "anki-design"
GROW = 80

RECT_JS = r"""
(function () {
  var a = document.querySelector('.ba-side');
  if (!a) return null;
  var b = a.getBoundingClientRect();
  return [b.left, b.top, b.width, b.height];
})()
"""

# Record the rail width seen on every mousemove of the drag: proves the
# width follows the pointer live, not only after the release.
LOG_JS = r"""
(function () {
  window.__liveWidths = [];
  document.addEventListener('mousemove', function () {
    var a = document.querySelector('.ba-side');
    if (!a) return;
    var w = Math.round(a.getBoundingClientRect().width);
    var l = window.__liveWidths;
    if (l[l.length - 1] !== w) l.push(w);
  }, true);
  return true;
})()
"""

STATE_JS = r"""
(function () {
  var a = document.querySelector('.ba-side');
  var body = document.body;
  var row = document.querySelector('.ad-list-row');
  var cs = getComputedStyle(document.documentElement);
  return {
    railWidth: a ? Math.round(a.getBoundingClientRect().width) : null,
    bodyPad: Math.round(parseFloat(getComputedStyle(body).paddingLeft) || 0),
    rowLeft: row ? Math.round(row.getBoundingClientRect().left) : null,
    sideVar: (cs.getPropertyValue('--rf-side-w') || '').trim(),
  };
})()
"""

HANDLE_JS = r"""
(function () {
  var h = document.querySelector('.ba-side-resizer');
  if (!h) return {present: false};
  var b = h.getBoundingClientRect();
  var cs = getComputedStyle(h);
  return {present: true, hovered: h.matches(':hover'), cursor: cs.cursor,
          left: b.left, width: b.width, height: b.height};
})()
"""


def _widget_point(t, web, x, y):
    from aqt.qt import QPoint

    z = web.zoomFactor() or 1.0
    return QPoint(int(x * z), int(y * z))


def run(t):
    mw = t.mw
    web = mw.web
    t.wait_js("document.querySelectorAll('.ad-list-row').length > 0", timeout=20)
    t.pump(400)  # let the rail's open transitions settle

    rect = t.js(RECT_JS)
    if not rect:
        t.check("the rail is on screen", False, "no .ba-side")
        return
    before = t.js(STATE_JS)
    t.note(f"before: {before}")
    left, top, width, height = rect
    edge_x = left + width - 2
    mid_y = top + height / 2

    # 1. Hover the right edge: a resize handle must be there and show the
    #    resize cursor to the user.
    p0 = _widget_point(t, web, edge_x, mid_y)
    Q = t.QTest
    if Q is None:
        t.check("QTest available", False, "")
        return
    from aqt.qt import Qt

    win = web.window().windowHandle()
    src = web.focusProxy() or web
    w0 = src.mapTo(web.window(), p0)
    Q.mouseMove(win, w0)
    t.pump(250)
    handle = t.js(HANDLE_JS) or {"present": False}
    t.note(f"handle under pointer: {handle}")
    t.check("hovering the rail's right edge shows a resize handle",
            handle.get("present") and handle.get("hovered")
            and handle.get("cursor") in ("col-resize", "ew-resize"),
            handle)

    # 2. Drag the edge 80px to the right with the real mouse.
    t.js(LOG_JS)
    p1 = _widget_point(t, web, edge_x + GROW, mid_y)
    t.native_drag(web, p0, p1)
    t.pump(400)  # padding/width transitions (200ms) and the pycmd round trip
    widths = t.js("JSON.stringify(window.__liveWidths || [])") or "[]"
    t.note(f"rail widths seen during the drag: {widths}")
    import json

    try:
        seen = json.loads(widths)
    except Exception:
        seen = []
    distinct = sorted(set(seen))
    t.check("the rail width follows the pointer during the drag",
            len(distinct) >= 3 and seen == sorted(seen),
            f"widths seen {seen}")

    after = t.js(STATE_JS)
    t.note(f"after: {after}")
    want = round(width) + GROW
    t.check(f"after the drag the rail is {GROW}px wider",
            abs((after.get("railWidth") or 0) - want) <= 2,
            f"rail {before.get('railWidth')} -> {after.get('railWidth')}, want {want}")
    t.check("the page content shifts right with the rail",
            abs((after.get("bodyPad") or 0) - want) <= 2
            and (after.get("rowLeft") or 0) - (before.get("rowLeft") or 0) >= GROW - 2,
            f"body padding {before.get('bodyPad')} -> {after.get('bodyPad')}, "
            f"first row left {before.get('rowLeft')} -> {after.get('rowLeft')}")

    # 3. Persisted: the add-on config holds the width and the Python helper
    #    the Qt overlays use agrees with the page.
    t.wait_until(lambda: (mw.addonManager.getConfig(ADDON) or {}).get("sidebar_width") == want,
                 timeout=5)
    cfg = mw.addonManager.getConfig(ADDON) or {}
    t.check("the width is saved in the add-on config",
            cfg.get("sidebar_width") == want,
            f"sidebar_width {cfg.get('sidebar_width')!r}, want {want}")
    py_w = None
    try:
        mod = next(m for k, m in sys.modules.items()
                   if k.startswith(ADDON) and k.endswith(".addcard"))
        py_w = mod.sidebar_w()
    except Exception as e:
        py_w = repr(e)
    t.check("the Qt overlays' sidebar width matches the page",
            py_w == want, f"addcard.sidebar_w() {py_w!r}, want {want}")

    # 4. A re-render of the home page keeps the new width (bootstrapped
    #    from the config, not from the DOM that was just wiped).
    mw.deckBrowser.refresh()
    t.wait_js("document.querySelectorAll('.ad-list-row').length > 0", timeout=20)
    t.pump(400)
    again = t.js(STATE_JS)
    t.check("the width survives a re-render of the deck list",
            abs((again.get("railWidth") or 0) - want) <= 2
            and abs((again.get("bodyPad") or 0) - want) <= 2,
            f"after refresh rail {again.get('railWidth')}, body padding "
            f"{again.get('bodyPad')}, want {want}")

    # 5. Collapsing still gives the thin rail, and expanding restores the
    #    user's width rather than the stock 264px.
    t.js("window.__baToggleSidebar && window.__baToggleSidebar()")
    t.pump(400)
    collapsed = t.js(STATE_JS)
    t.js("window.__baToggleSidebar && window.__baToggleSidebar()")
    t.pump(400)
    expanded = t.js(STATE_JS)
    t.check("collapse and expand keep the chosen width",
            collapsed.get("railWidth") == 64
            and abs((expanded.get("railWidth") or 0) - want) <= 2,
            f"collapsed {collapsed.get('railWidth')}, expanded {expanded.get('railWidth')}, "
            f"want {want}")

    # 6. With Browse open inline (a Qt overlay beside the rail, as in the
    #    user's screenshot), a drag on the edge must move the overlay too.
    embed = next((m for k, m in sys.modules.items()
                  if k.startswith(ADDON) and k.endswith(".browse_embed")), None)
    if embed is None:
        t.check("browse_embed is loaded", False, "")
        return
    mw.onBrowse()
    overlay = t.wait_until(lambda: embed._state.get("overlay"), timeout=20)
    t.wait_until(lambda: embed._state.get("curtain") is None, timeout=10)
    t.pump(300)
    t.check("Browse opens inline at the rail's edge",
            overlay is not None and overlay.geometry().x() == want,
            f"overlay x {overlay.geometry().x() if overlay else None}, want {want}")
    rect = t.js(RECT_JS)
    left, top, width, height = rect
    edge_x = left + width - 2
    mid_y = top + height / 2
    p0 = _widget_point(t, web, edge_x, mid_y)
    p1 = _widget_point(t, web, edge_x - 40, mid_y)
    t.native_drag(web, p0, p1)
    t.pump(500)
    want2 = want - 40
    st = t.js(STATE_JS)
    cfg = mw.addonManager.getConfig(ADDON) or {}
    t.check("with Browse open, dragging the edge narrows the rail",
            abs((st.get("railWidth") or 0) - want2) <= 2 and cfg.get("sidebar_width") == want2,
            f"rail {st.get('railWidth')}, config {cfg.get('sidebar_width')}, want {want2}")
    t.check("the Browse overlay moves to the new edge without a window resize",
            overlay.geometry().x() == want2
            and overlay.geometry().width() == mw.form.centralwidget.width() - want2,
            f"overlay geometry {overlay.geometry()}, want x {want2}")
