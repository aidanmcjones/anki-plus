import json
import os

import pytest

from ankibug import schema, store


def test_write_and_read_roundtrip(tmp_path):
    root = str(tmp_path)
    t = schema.new_ticket("editor loses focus", kind="app")
    tdir = store.write_ticket(t, root=root, screenshot_bytes=b"\x89PNGfake",
                               qa_html="<div id='qa'>hi</div>", log_text="capture: ok")

    assert tdir == os.path.join(root, t["id"])
    assert os.path.isfile(os.path.join(tdir, "ticket.json"))
    assert os.path.isfile(os.path.join(tdir, "screenshot.png"))
    assert os.path.isfile(os.path.join(tdir, "qa.html"))
    assert os.path.isfile(os.path.join(tdir, "capture.log"))

    got = store.read_ticket(t["id"], root=root)
    assert got == t


def test_write_ticket_rejects_invalid(tmp_path):
    t = schema.new_ticket("note")
    t["status"] = "not-a-status"
    with pytest.raises(schema.SchemaError):
        store.write_ticket(t, root=str(tmp_path))


def test_list_tickets_sorted(tmp_path):
    root = str(tmp_path)
    ids = []
    for i in range(3):
        t = schema.new_ticket("note {}".format(i),
                               ticket_id="2026010{}-000000-note-{}".format(i + 1, i))
        store.write_ticket(t, root=root)
        ids.append(t["id"])

    listed = [t["id"] for t in store.list_tickets(root=root)]
    assert listed == sorted(ids)


def test_update_status_and_index(tmp_path):
    root = str(tmp_path)
    t = schema.new_ticket("scroll jumps in reviewer", kind="app")
    store.write_ticket(t, root=root)

    updated = store.update_status(t["id"], "fixing", root=root)
    assert updated["status"] == "fixing"

    reread = store.read_ticket(t["id"], root=root)
    assert reread["status"] == "fixing"

    index_text = open(store.index_path(root)).read()
    assert t["id"] in index_text
    assert "fixing" in index_text
    assert "scroll jumps in reviewer" in index_text


def test_update_status_rejects_bad_value(tmp_path):
    root = str(tmp_path)
    t = schema.new_ticket("note")
    store.write_ticket(t, root=root)
    with pytest.raises(schema.SchemaError):
        store.update_status(t["id"], "bogus", root=root)


def test_set_field_top_level(tmp_path):
    root = str(tmp_path)
    t = schema.new_ticket("note")
    store.write_ticket(t, root=root)
    updated = store.set_field(t["id"], "status", "wontfix", root=root)
    assert updated["status"] == "wontfix"


def test_set_field_nested(tmp_path):
    root = str(tmp_path)
    t = schema.new_ticket("note", reviewer={"card_id": 1, "note_id": 2})
    store.write_ticket(t, root=root)
    updated = store.set_field(t["id"], "reviewer.deck", "CourseB::Unit3", root=root)
    assert updated["reviewer"]["deck"] == "CourseB::Unit3"


def test_regenerate_index_escapes_pipes(tmp_path):
    root = str(tmp_path)
    t = schema.new_ticket("note with a | pipe character")
    store.write_ticket(t, root=root)
    text = open(store.index_path(root)).read()
    assert "\\|" in text


def test_regenerate_index_empty_root(tmp_path):
    root = str(tmp_path / "empty")
    path = store.regenerate_index(root=root)
    text = open(path).read()
    assert "id | created" in text
    assert store.list_tickets(root=root) == []
