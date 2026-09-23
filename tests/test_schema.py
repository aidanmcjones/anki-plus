import datetime

import pytest

from ankibug import schema


def test_slugify_basic():
    assert schema.slugify("Editor crashes on Ctrl+Z!!") == "editor-crashes-on-ctrl-z"


def test_slugify_empty():
    assert schema.slugify("") == "ticket"
    assert schema.slugify("   ---   ") == "ticket"


def test_make_id_format():
    when = datetime.datetime(2026, 9, 22, 21, 45, 3)
    tid = schema.make_id("Deck build failed", when)
    assert tid == "20260922-214503-deck-build-failed"


def test_new_ticket_is_valid():
    t = schema.new_ticket("something broke", source="terminal", kind="app")
    schema.validate(t)  # should not raise
    assert t["schema"] == 1
    assert t["status"] == "new"
    assert t["capture"] is None
    assert t["deck_build"] is None
    assert t["fix"] is None
    assert t["app"]["addon_repo"] == "~/dev/anki-design"
    assert t["reviewer"]["state"] is None


def test_new_ticket_custom_id_and_overrides():
    t = schema.new_ticket(
        "note",
        ticket_id="20260101-000000-custom",
        app={"anki_version": "26.08.1"},
        reviewer={"card_id": 42, "note_id": 7, "state": "question"},
    )
    assert t["id"] == "20260101-000000-custom"
    assert t["app"]["anki_version"] == "26.08.1"
    assert t["reviewer"]["card_id"] == 42
    assert t["reviewer"]["state"] == "question"


@pytest.mark.parametrize("field,value", [
    ("source", "carrier-pigeon"),
    ("status", "banana"),
    ("kind", "spaceship"),
])
def test_invalid_enum_rejected(field, value):
    t = schema.new_ticket("note")
    t[field] = value
    with pytest.raises(schema.SchemaError):
        schema.validate(t)


def test_missing_required_field():
    t = schema.new_ticket("note")
    del t["created"]
    with pytest.raises(schema.SchemaError):
        schema.validate(t)


def test_bad_schema_version():
    t = schema.new_ticket("note")
    t["schema"] = 2
    with pytest.raises(schema.SchemaError):
        schema.validate(t)


def test_bad_created_timestamp():
    t = schema.new_ticket("note")
    t["created"] = "not-a-date"
    with pytest.raises(schema.SchemaError):
        schema.validate(t)


def test_capture_shape_validated():
    t = schema.new_ticket("note", capture={
        "pages": [{"title": "main webview", "url": "http://x"}],
        "qa_html_path": "qa.html",
        "visible_text": "hi",
        "console_errors": [],
        "screenshot_path": "screenshot.png",
        "captured_at": datetime.datetime.now().astimezone().isoformat(),
    })
    schema.validate(t)

    bad = schema.new_ticket("note")
    bad["capture"] = {"pages": "not-a-list"}
    with pytest.raises(schema.SchemaError):
        schema.validate(bad)


def test_is_valid_helper():
    good = schema.new_ticket("note")
    assert schema.is_valid(good)
    bad = dict(good)
    bad["status"] = "nope"
    assert not schema.is_valid(bad)
