"""Live: tags file themselves under their course's root.

Request (2026-09-24): "what about tag organization for every class and deck
and every future class and deck?" Every tag should live under its course's
root (`FunBiochem::...`, `MCAT::...`), including tags typed by hand in the
editor or Browse and tags that arrive with imported decks.

Sets `tag_roots` to {"Parent": "Parent"} (Solo is left unmapped, so it gets
the derived root "Solo"), then drives the real app: adds a note through the
real Add screen with a flat tag, tags notes through Browse's own
`add_tags_to_selected_notes`, and tags notes through Anki's tag ops for the
other cases. Checks what the user would see in the collection: the tag
renamed under the course root, shared and ignored tags left alone, cards
in a filtered deck counted for their home deck, Tools > Organize Tags Now,
the `auto_organize_tags` switch, one undo step per pass, and that no field
or scheduling value changed.

Fixture: Parent::{A,B,C} with two new Basic cards each, and Solo.
"""

import sys

ADDON = "anki-design"


def _module(suffix):
    for name, mod in list(sys.modules.items()):
        if mod is not None and name.startswith("anki-design") and name.endswith(suffix):
            return mod
    return None


def run(t):
    import aqt
    import aqt.utils
    from aqt.operations.tag import add_tags_to_notes

    mw = t.mw
    col = mw.col

    cfg = mw.addonManager.getConfig(ADDON) or {}
    cfg["auto_organize_tags"] = True
    cfg["tag_roots"] = {"Parent": "Parent"}
    cfg["tag_organize_ignore"] = ["Type", "AnkiHub_Subdeck", "marked", "leech"]
    mw.addonManager.writeConfig(ADDON, cfg)

    tips = []
    orig_tip = aqt.utils.tooltip

    def fake_tip(msg, *a, **k):
        tips.append(str(msg))
        return orig_tip(msg, *a, **k)

    aqt.utils.tooltip = fake_tip

    def nids(search):
        return sorted({col.get_card(c).nid for c in col.find_cards(search)})

    def tags_of(nid):
        return sorted(col.get_note(nid).tags)

    def has_tag(name):
        return any(x.casefold() == name.casefold() for x in col.tags.all())

    def tag_op(note_ids, tags):
        add_tags_to_notes(parent=mw, note_ids=note_ids, space_separated_tags=tags).run_in_background()

    def settle(ms=1400):
        # debounce is 500 ms; give the rename op time to finish too
        t.pump(ms)

    def sched_snapshot():
        return {
            r[0]: tuple(r[1:])
            for r in col.db.all(
                "select id, nid, did, odid, ord, type, queue, due, odue, ivl, factor, reps, lapses, left from cards"
            )
        }

    def fields_snapshot():
        return {r[0]: r[1] for r in col.db.all("select id, flds from notes")}

    try:
        # let the profile-open pass happen first
        t.pump(2200)
        pa, pb, pc = nids('deck:"Parent::A"'), nids('deck:"Parent::B"'), nids('deck:"Parent::C"')
        solo = nids('deck:"Solo"')

        # a filtered deck holding Parent::C's cards (home deck resolution)
        fdid = 0
        try:
            fd = col.sched.get_or_create_filtered_deck(0)
            fd.name = "Restudy"
            fd.allow_empty = True
            del fd.config.search_terms[:]
            term = fd.config.search_terms.add()
            term.search = 'deck:"Parent::C"'
            term.limit = 100
            fdid = col.sched.add_or_update_filtered_deck(fd).id
        except Exception as e:
            t.note(f"filtered deck build failed: {e!r}")
        in_filtered = col.find_cards(f"did:{fdid}") if fdid else []
        t.check("Parent::C's cards sit in a filtered deck", len(in_filtered) == 2, len(in_filtered))

        fields0 = fields_snapshot()
        sched0 = sched_snapshot()

        # --- 1. the real Add screen, flat tag ------------------------------
        before_notes = set(col.db.list("select id from notes"))
        added_nid = None
        mw.onAddCard()
        def find_add():
            # the add-on puts Add in Browse's card panel; fall back to its
            # full-pane embed, then Anki's own window
            for suffix, key in ((".browse_embed", "pane_add_ac"), (".addcard_embed", "addcards")):
                m = _module(suffix)
                if m is not None and m._state.get(key) is not None:
                    return m._state.get(key)
            return aqt.dialogs._dialogs.get("AddCards", [None, None])[1]

        ac = t.wait_until(find_add, timeout=15)
        t.check("the Add screen opens", ac is not None, "")
        if ac is not None:
            t.wait_until(lambda: getattr(ac.editor, "note", None) is not None, timeout=10)
            ac.deck_chooser.selected_deck_id = col.decks.id_for_name("Parent::A")
            note = ac.editor.note
            note.fields[0] = "organizer front"
            note.fields[1] = "organizer back"
            note.tags = ["hi_yield"]
            ac.editor.loadNote()
            t.pump(800)
            ac.add_current_note()
            got = t.wait_until(lambda: set(col.db.list("select id from notes")) - before_notes, timeout=10)
            added_nid = next(iter(got)) if got else None
            t.check("the Add screen added the note", added_nid is not None, "")
        if added_nid:
            ok = t.wait_until(lambda: "Parent::hi_yield" in tags_of(added_nid), timeout=5)
            t.check("a flat tag typed on Add is filed as Parent::hi_yield",
                    ok, tags_of(added_nid))
            t.check("the flat tag is gone from the tag list", not has_tag("hi_yield"), col.tags.all())
            t.check("the tooltip names the move",
                    any("Filed 1 tag under Parent::" in x for x in tips), tips)
            t.check("the rename is one undo step named Organize Tags",
                    col.undo_status().undo == "Organize Tags", col.undo_status().undo)

        # --- 2. Browse: add a tag to selected notes -------------------------
        mw.onBrowse()
        embed = _module(".browse_embed")
        br = t.wait_until(
            lambda: (embed and embed._state.get("browser")) or aqt.dialogs._dialogs.get("Browser", [None, None])[1],
            timeout=20,
        )
        t.check("Browse opens", br is not None, "")
        if br is not None:
            t.pump(1000)
            br.search_for('deck:"Parent::B"')
            t.pump(300)
            br.table.select_all()
            t.pump(200)
            sel = sorted(br.selected_notes())
            t.check("Browse selects Parent::B's notes", sel == pb, (sel, pb))
            br.add_tags_to_selected_notes(tags="browse_tag")
            ok = t.wait_until(lambda: all("Parent::browse_tag" in tags_of(n) for n in pb), timeout=5)
            t.check("a tag added in Browse is filed as Parent::browse_tag",
                    ok, [tags_of(n) for n in pb])
            t.check("browse_tag is gone from the tag list", not has_tag("browse_tag"), col.tags.all())

        # --- 3. several cases at once through Anki's tag op ------------------
        tag_op(pc + solo, "shared")
        tag_op(pa, "marked leech Type::X AnkiHub_Subdeck::X AnkiHub_Thing")
        tag_op(solo, "solo_tag")
        tag_op(pc, "filt_tag")
        tag_op(pa, "Kaplan::Ch1")
        settle(1800)
        t.check("a tag on Parent and Solo notes stays put",
                all("shared" in tags_of(n) for n in pc + solo) and not has_tag("Parent::shared")
                and not has_tag("Solo::shared"),
                [tags_of(n) for n in pc + solo])
        want = ["AnkiHub_Subdeck::X", "AnkiHub_Thing", "Type::X", "leech", "marked"]
        t.check("marked, leech, Type::X, AnkiHub_Subdeck::X and AnkiHub tags never move",
                all(all(w in tags_of(n) for w in want) for n in pa)
                and not any(has_tag(f"Parent::{w}") for w in want),
                [tags_of(n) for n in pa])
        t.check("an unmapped top-level deck gets a derived root (Solo::solo_tag)",
                all("Solo::solo_tag" in tags_of(n) for n in solo) and not has_tag("solo_tag"),
                [tags_of(n) for n in solo])
        t.check("cards in a filtered deck count for their home deck (Parent::filt_tag)",
                all("Parent::filt_tag" in tags_of(n) for n in pc),
                [tags_of(n) for n in pc])
        t.check("a foreign nested tag is nested under the root (Parent::Kaplan::Ch1)",
                all("Parent::Kaplan::Ch1" in tags_of(n) for n in pa) and not has_tag("Kaplan::Ch1"),
                [tags_of(n) for n in pa])

        # a tag the user renames by hand (the sidebar's Rename, or a drag to
        # the top level) is not moved straight back
        from aqt.operations.tag import rename_tag
        if has_tag("Parent::browse_tag"):
            # (only when there is something to rename: an empty rename
            # opens a blocking info box)
            rename_tag(parent=mw, current_name="Parent::browse_tag", new_name="browse_top").run_in_background()
            settle(1800)
        t.check("a tag renamed by hand to the top level stays there",
                all("browse_top" in tags_of(n) for n in pb) and not has_tag("Parent::browse_top"),
                [tags_of(n) for n in pb])

        # --- 4. switched off, then Organize Tags Now -------------------------
        cfg = mw.addonManager.getConfig(ADDON) or {}
        cfg["auto_organize_tags"] = False
        mw.addonManager.writeConfig(ADDON, cfg)
        tag_op(pb, "later")
        tag_op(pa, "later_too::x")
        settle(1800)
        t.check("with auto_organize_tags off, a new tag stays where it is",
                all("later" in tags_of(n) for n in pb) and not has_tag("Parent::later"),
                [tags_of(n) for n in pb])

        act = None
        try:
            for a in mw.form.menuTools.actions():
                if a.text().replace("&", "") == "Organize Tags Now":
                    act = a
        except Exception:
            pass
        t.check("Tools has Organize Tags Now", act is not None,
                [a.text() for a in mw.form.menuTools.actions()])
        if act is not None:
            tips.clear()
            act.trigger()
            ok = t.wait_until(lambda: all("Parent::later" in tags_of(n) for n in pb)
                              and all("Parent::browse_top" in tags_of(n) for n in pb)
                              and all("Parent::later_too::x" in tags_of(n) for n in pa), timeout=5)
            t.check("Organize Tags Now files every tag in one full pass, hand-renamed ones too", ok,
                    [tags_of(n) for n in pa + pb])
            t.wait_until(lambda: tips, timeout=3)
            t.check("it reports what moved and what stayed",
                    any("Filed 3 tags under Parent::" in x and "several courses" in x for x in tips), tips)
            t.check("shared is still left alone after a full pass",
                    all("shared" in tags_of(n) for n in pc + solo), [tags_of(n) for n in pc + solo])
            tips.clear()
            act.trigger()
            t.wait_until(lambda: tips, timeout=3)
            t.check("a second full pass says all is organized",
                    any("already organized" in x for x in tips), tips)

        # --- 5. nothing but tags changed ------------------------------------
        fields1 = fields_snapshot()
        sched1 = sched_snapshot()
        if added_nid:
            fields1.pop(added_nid, None)
            for cid in [c for c, v in sched1.items() if v[0] == added_nid]:
                sched1.pop(cid)
        t.check("no note field changed", fields1 == fields0,
                [k for k in fields0 if fields0.get(k) != fields1.get(k)])
        t.check("no card's deck or scheduling changed", sched1 == sched0,
                [k for k in sched0 if sched0.get(k) != sched1.get(k)])
        t.note(f"final tags: {sorted(col.tags.all())}")
    finally:
        aqt.utils.tooltip = orig_tip
