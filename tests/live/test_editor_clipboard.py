"""Live: the editor image menu's Copy image / Cut image / paste-into-field
(d04d4ad, ticket 20260923-102640).

The unit tests (tests/test_editor_clipboard.py) prove editor_tools.py's
storage/dispatch logic against a stand-in aqt.qt. This test drives the real
reviewer webview inside the real app and checks what the user would see:
the OS clipboard actually holding the bitmap, the image actually leaving
the field on Cut, and a pasted image actually landing in collection.media
with an <img> in the field -- never a data: URL.

Surface driven: the reviewer's inline "quick editor" (editreviewer.py +
web/reviewer.js's window.__baEnterEdit), reached the same way the user
reaches it -- mw.onEditCurrent(), the exact function the pencil button and
the 'e' shortcut are patched to call. Not Anki's native fields Editor
(Browse/Add/Edit Current): that page's fields are Svelte web components
behind closed shadow roots, and — per the diff's own comment — "Anki's full
editor bridges paste to Python itself (Editor.onPaste stores clipboard
images), so only the quick editor needs this": ba:image-paste is only wired
into the quick editor's plain-DOM fields. Copy/Cut share one JS function and
one Python dispatch (editor_tools.handle) between both surfaces, so
exercising them here exercises the same code the full editor's image menu
runs.

Gap (documented, not skipped): offscreen can't show the native OS context
menu. So instead of a real right-click + menu, the test dispatches the same
'contextmenu' DOM event the app's own listener (document.addEventListener
("contextmenu", ...) in web/editor-tools.js) reacts to, which builds the
real #ba-image-menu DOM and wires its buttons with the real click handlers
copySelectedImage(false/true) -- then clicks those real buttons. Everything
from there (imageToPng, the "ba:image-copy:" pycmd, editor_tools.copy_image,
QApplication.clipboard()) is the genuine app code path.

Paste is exercised by dispatching a real 'paste' DOM event at the field
with a synthetic clipboardData (a File built from PNG bytes) rather than a
live OS-level Cmd/Ctrl+V, since driving Chromium's native clipboard-backed
paste through QtWebEngine offscreen isn't reliably scriptable. Downstream of
that event everything is real: pasteIntoField's FileReader, the
"ba:image-paste:" pycmd, editor_tools.save_pasted_image, media.write_data,
and insertImage's caret-relative <img> insertion. A real QImage is also put
on the system clipboard first (of the same size) so the setup matches what
a user's OS clipboard would hold; the synthetic paste's payload is built
from that same PNG's bytes so the two agree.
"""

import base64
import json
import os


def run(t):
    from aqt.qt import QApplication, QBuffer, QByteArray, QColor, QIODevice, QImage

    mw = t.mw
    col = mw.col

    # -- 1. Fixture: a note with an image in its Back field, in its own deck. #
    COPY_W, COPY_H = 9, 5

    def make_png(w, h, color):
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(color)
        buf = QByteArray()
        dev = QBuffer(buf)
        dev.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(dev, "PNG")
        dev.close()
        return bytes(buf)

    fixture_png = make_png(COPY_W, COPY_H, QColor(30, 90, 200))
    fixture_name = col.media.write_data("clip-fixture.png", fixture_png)
    t.note(f"fixture media file: {fixture_name}, {len(fixture_png)} bytes")

    did = col.decks.id("ClipboardTest")
    model = col.models.by_name("Basic")
    note = col.new_note(model)
    note["Front"] = "Clipboard fixture"
    note["Back"] = f'<img src="{fixture_name}">'
    col.add_note(note, did)
    card_id = note.cards()[0].id
    col.decks.select(did)

    # -- Open the fixture card in review, the way the user would. --------- #
    mw.moveToState("overview")
    t.wait_until(lambda: getattr(mw, "state", "") == "overview", timeout=10)
    t.pump(300)
    mw.moveToState("review")
    shown = t.wait_until(
        lambda: getattr(mw, "state", "") == "review"
        and getattr(mw.reviewer, "card", None) is not None
        and mw.reviewer.card.id == card_id,
        timeout=15,
    )
    t.check("reviewer shows the fixture card", bool(shown),
            (getattr(mw, "state", None), getattr(getattr(mw.reviewer, "card", None), "id", None)))
    if not shown:
        return
    # Give WebEngine a moment to finish navigating to the review page before
    # the first eval -- an eval sent while the page is still being replaced
    # can hang until timeout with no callback at all.
    t.pump(1000)

    def js_soon(code, timeout=20.0):
        return t.wait_until(lambda: t.js(code, timeout=2.0), timeout=timeout, step_ms=150)

    # -- Enter the quick editor the way the pencil button / 'e' do. ------- #
    # reviewer.js (which defines window.__baEnterEdit) loads async with the
    # rest of the card HTML; onEditCurrent's JS-side fallback silently opens
    # Anki's real EditCurrent dialog instead if it isn't there yet, so wait
    # for it explicitly rather than racing.
    have_hook = js_soon("typeof window.__baEnterEdit === 'function'")
    if not have_hook:
        t.note(f"scripts on page: {js_soon('Array.from(document.scripts).map(function(s){return s.src;})')}")
        t.note(f"js errors: {js_soon('window.__baErrors')}")
        t.note(f"body class: {js_soon('document.body.className')}")
    t.check("the reviewer's inline-edit hook (web/reviewer.js) is loaded",
            bool(have_hook), have_hook)
    if not have_hook:
        return
    mw.onEditCurrent()
    editing = t.wait_until(
        lambda: t.js("document.body.classList.contains('ba-editing')"), timeout=15)
    t.check("inline quick editor opens on the fixture card", bool(editing),
            t.js("document.body.className"))
    if not editing:
        return

    has_field_img = t.wait_until(
        lambda: t.js("!!document.querySelector('[data-ba-field=\"Back\"] img')"), timeout=5)
    t.check("the fixture image is present in the editable Back field",
            bool(has_field_img),
            t.js("Array.from(document.querySelectorAll('[data-ba-field]'))"
                 ".map(function(e){return e.getAttribute('data-ba-field');})"))
    if not has_field_img:
        return

    # -- Helper: dispatch the real 'contextmenu' event the app listens for #
    # (the same event a real right-click sends), then click the real menu #
    # button by its label -- the native OS menu itself is what offscreen  #
    # cannot show; everything downstream of that event is the app's own  #
    # code, unmodified.                                                   #
    def menu_action(label):
        code = (
            "(function(action){"
            "var img=document.querySelector('[data-ba-field=\"Back\"] img');"
            "if(!img) return {ok:false, reason:'no-img'};"
            "var r=img.getBoundingClientRect();"
            "img.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,"
            "composed:true,clientX:r.left+1,clientY:r.top+1,view:window}));"
            "var menu=document.getElementById('ba-image-menu');"
            "if(!menu||menu.hidden) return {ok:false, reason:'no-menu'};"
            "var btns=Array.prototype.slice.call(menu.querySelectorAll('button'));"
            "var labels=btns.map(function(b){return b.textContent;});"
            "var btn=btns.filter(function(b){return b.textContent===action;})[0];"
            "if(!btn) return {ok:false, reason:'no-button', labels:labels};"
            "btn.click();"
            "return {ok:true, labels:labels};"
            "})(" + json.dumps(label) + ")"
        )
        return t.js(code)

    clipboard = QApplication.clipboard()

    # -- 2. Copy image: the same pycmd path the context menu button runs. - #
    clipboard.clear()
    r = menu_action("Copy image")
    t.note(f"Copy image menu result: {r}")
    t.check("the image menu offers Copy image and it is clickable",
            bool(r and r.get("ok")), r)
    got_copy = t.wait_until(lambda: not clipboard.image().isNull(), timeout=5)
    copy_img = clipboard.image()
    t.check("system clipboard holds an image after Copy image",
            bool(got_copy), (copy_img.width(), copy_img.height()))
    t.check("the copied clipboard image is the field image's size",
            (copy_img.width(), copy_img.height()) == (COPY_W, COPY_H),
            (copy_img.width(), copy_img.height()))
    still_there = t.js("!!document.querySelector('[data-ba-field=\"Back\"] img')")
    t.check("Copy image leaves the field's image in place", bool(still_there), still_there)

    # -- 3. Cut image: clipboard still gets it, but the field loses it. --- #
    clipboard.clear()
    r = menu_action("Cut image")
    t.note(f"Cut image menu result: {r}")
    t.check("the image menu offers Cut image and it is clickable",
            bool(r and r.get("ok")), r)
    got_cut = t.wait_until(lambda: not clipboard.image().isNull(), timeout=5)
    cut_img = clipboard.image()
    t.check("system clipboard holds an image after Cut image",
            bool(got_cut), (cut_img.width(), cut_img.height()))
    t.check("the cut clipboard image is the field image's size",
            (cut_img.width(), cut_img.height()) == (COPY_W, COPY_H),
            (cut_img.width(), cut_img.height()))
    gone = t.wait_until(
        lambda: not t.js("!!document.querySelector('[data-ba-field=\"Back\"] img')"), timeout=5)
    back_html_after_cut = t.js(
        "(document.querySelector('[data-ba-field=\"Back\"]')||{}).innerHTML || ''")
    t.check("Cut image removes the <img> from the field HTML", bool(gone), back_html_after_cut)

    # -- 4. Paste: a fresh image on the clipboard lands in the field and in #
    # collection.media, never as a data: URL.                              #
    PASTE_W, PASTE_H = 11, 7
    paste_png = make_png(PASTE_W, PASTE_H, QColor(210, 40, 40))
    clipboard.setImage(QImage.fromData(paste_png, "PNG"))
    t.note(f"put a fresh {PASTE_W}x{PASTE_H} image on the system clipboard for paste")

    media_dir = col.media.dir()
    before_files = set(os.listdir(media_dir))

    paste_b64 = base64.b64encode(paste_png).decode()
    paste_code = (
        "(function(b64){"
        "var field=document.querySelector('[data-ba-field=\"Back\"]');"
        "if(!field) return {ok:false, reason:'no-field'};"
        "field.focus({preventScroll:true});"
        "try{var range=document.createRange();range.selectNodeContents(field);"
        "range.collapse(false);var sel=window.getSelection();sel.removeAllRanges();"
        "sel.addRange(range);}catch(_){}"
        "var bin=atob(b64);var bytes=new Uint8Array(bin.length);"
        "for(var i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);"
        "var file=new File([bytes], 'clip.png', {type:'image/png'});"
        "var dt=new DataTransfer(); dt.items.add(file);"
        "var evt=new ClipboardEvent('paste', {bubbles:true, cancelable:true, composed:true,"
        "clipboardData: dt});"
        "var prevented=field.dispatchEvent(evt);"
        "return {ok:true, defaultPrevented: !prevented};"
        "})(" + json.dumps(paste_b64) + ")"
    )
    r = t.js(paste_code)
    t.note(f"paste dispatch result: {r}")
    t.check("the field's paste handler accepted the synthetic clipboard image",
            bool(r and r.get("ok") and r.get("defaultPrevented")), r)

    new_file = t.wait_until(
        lambda: (set(os.listdir(media_dir)) - before_files) or None, timeout=5)
    after_files = set(os.listdir(media_dir))
    added = after_files - before_files
    t.check("a new file appeared in collection.media for the pasted image",
            len(added) == 1, sorted(added))
    added_name = next(iter(added)) if added else None
    t.check("the new media file is named like a paste (paste-<hash>.<ext>)",
            bool(added_name and added_name.startswith("paste-")), added_name)
    if added_name:
        on_disk = open(os.path.join(media_dir, added_name), "rb").read()
        t.check("the stored file's bytes are exactly the pasted PNG",
                on_disk == paste_png, len(on_disk))

    field_img_src = t.wait_until(
        lambda: t.js(
            "(function(){var i=document.querySelector('[data-ba-field=\"Back\"] img');"
            "return i ? decodeURIComponent(i.getAttribute('src')||'') : null;})()"),
        timeout=5,
    )
    t.check("an <img> pointing at the new file was inserted in the field",
            field_img_src == added_name, (field_img_src, added_name))

    # -- 5. Nothing anywhere in the field was written as a data: URL. ----- #
    back_html_after_paste = t.js(
        "(document.querySelector('[data-ba-field=\"Back\"]')||{}).innerHTML || ''")
    t.check("no data: URL image was written into the field",
            "data:image" not in back_html_after_paste, back_html_after_paste)

    # -- Bonus: save and confirm the round trip landed in the collection. - #
    saved = t.js(
        "(function(){var b=document.querySelector('#ba-edit-bar [data-action=\"save\"]');"
        "if(!b) return false; b.click(); return true;})()"
    )
    t.check("the quick editor's Save button is present and clickable", bool(saved), saved)
    exited = t.wait_until(
        lambda: not t.js("document.body.classList.contains('ba-editing')"), timeout=5)
    t.check("saving exits the quick editor", bool(exited), "")
    if added_name:
        col_after = col.get_note(note.id)
        back_val = col_after["Back"]
        t.check("the saved note's Back field references the pasted media file",
                added_name in back_val, back_val)
        t.check("the saved note's Back field has no data: URL either",
                "data:image" not in back_val, back_val)
