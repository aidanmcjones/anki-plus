"""Regression tests for the post-deadline mode resolution in deadlines.py.

What a deck does once its deadline is behind it used to be a hardcoded
`MODE_MAINTAIN` fallback repeated at four call sites, with `set_deadline`
materialising that fallback into every entry as though the user had picked
it. It is now the `deadline_passed_mode` config key (default "pause"),
resolved in one place (`entry_mode`), with an explicit per-deck choice
still winning.

Covered here:
  (a) an explicit per-deck "maintain" wins over a "pause" config default;
  (b) an explicit per-deck "pause" wins over a "maintain" config default;
  (c) an entry with no `mode` key at all falls back to the config default;
  (d) config set to "maintain" restores the old behavior wholesale;
  (e) the one-time migration flips only already-passed, never-chosen
      entries, and is idempotent.

Runs standalone (no Anki install needed):
`python3 tests/test_deadline_modes.py`. Stubs aqt before import, same
approach as test_heatmap_window.py, and drives a fake collection whose
decks/presets are plain dicts — everything under test here is dict and
date logic.
"""

import copy
import datetime
import os
import sys
import types


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


class _AddonManager:
    """Stands in for Anki's, including the bit that matters: getConfig
    hands back a *copy*, so a mutation only survives if someone actually
    calls writeConfig."""

    def __init__(self):
        self.cfg = {}

    def getConfig(self, *a, **k):
        return copy.deepcopy(self.cfg)

    def writeConfig(self, _addon, cfg):
        self.cfg = copy.deepcopy(cfg)


_addons = _AddonManager()
_mw = types.SimpleNamespace(col=None, addonManager=_addons)
_stub("aqt", mw=_mw)
_stub("aqt.utils", tooltip=lambda *a, **k: None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "ba_deadlines",
    os.path.join(os.path.dirname(__file__), "..", "deadlines.py"),
)
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)

PAST = "2020-01-01"      # comfortably behind any clock this runs on
FUTURE = "2099-01-01"


# --------------------------------------------------------------------------- #
# Fake collection
# --------------------------------------------------------------------------- #
class _FakeDecks:
    def __init__(self, decks, confs):
        self.decks = decks
        self.confs = confs

    # Reads hand back copies and writes go through update_*, so a write
    # that never happens is visible to the test (and to
    # `_verify_max_interval`, which exists precisely to catch that).
    def get(self, did, default=True):
        deck = self.decks.get(int(did))
        if deck is None:
            return copy.deepcopy(self.decks[1]) if default else None
        return copy.deepcopy(deck)

    def update_dict(self, deck):
        self.decks[int(deck["id"])] = copy.deepcopy(deck)

    def name(self, did):
        return self.decks[int(did)]["name"]

    def all_names_and_ids(self):
        return [types.SimpleNamespace(id=i, name=self.decks[i]["name"]) for i in sorted(self.decks)]

    def config_dict_for_deck_id(self, did):
        return copy.deepcopy(self.confs[int(self.decks[int(did)]["conf"])])

    def get_config(self, cid):
        return copy.deepcopy(self.confs[int(cid)])

    def all_config(self):
        return [copy.deepcopy(c) for c in self.confs.values()]

    def update_config(self, conf):
        self.confs[int(conf["id"])] = copy.deepcopy(conf)

    def add_config_returning_id(self, name, clone_from=None):
        cid = max(self.confs) + 1
        conf = copy.deepcopy(clone_from) if clone_from else _preset(cid, name)
        conf["id"] = cid
        conf["name"] = name
        self.confs[cid] = conf
        return cid

    def set_config_id_for_deck_dict(self, deck, cid):
        self.decks[int(deck["id"])]["conf"] = int(cid)


def _preset(cid, name):
    return {
        "id": cid,
        "name": name,
        "new": {"perDay": 200},
        "rev": {"perDay": 2000, "maxIvl": 36500},
    }


def _fresh_collection():
    """Six decks, each already sitting on its own "— deadline" clone —
    the shape of the real collection this change was written for."""
    decks = {1: {"id": 1, "name": "Default", "conf": 1}}
    confs = {1: _preset(1, "Default")}
    for i, did in enumerate(DIDS, start=1):
        cid = 9000 + i
        decks[did] = {"id": did, "name": f"Deck {i}", "conf": cid}
        confs[cid] = _preset(cid, f"Default — Deck {i}{dl.CLONE_SUFFIX}")
    col = types.SimpleNamespace(decks=_FakeDecks(decks, confs), sched=types.SimpleNamespace(today=0))
    _mw.col = col
    return col


DIDS = [101, 102, 103, 201, 202, 203]


def _entry(date, mode=None, cloned=None):
    e = {
        "date": date,
        "orig_conf": 1,
        "orig_max_ivl": 36500,
        "orig_new_per_day": 200,
        "orig_rev_per_day": 2000,
        "notified_passed": True,
    }
    if cloned is not None:
        e["cloned_conf"] = cloned
    if mode is not None:
        e["mode"] = mode
    return e


def _setup(config_mode=None, state=None):
    """Reset the fake config + collection between tests."""
    cfg = {"deck_deadlines": True}
    if config_mode is not None:
        cfg[dl.PASSED_MODE_KEY] = config_mode
    if state is not None:
        cfg[dl.STATE_KEY] = copy.deepcopy(state)
    _addons.cfg = cfg
    return _fresh_collection()


def _state_now():
    return _addons.cfg.get(dl.STATE_KEY, {})


def _limits(col, did):
    deck = col.decks.decks[did]
    return (deck.get("newLimit"), deck.get("reviewLimit"))


# --------------------------------------------------------------------------- #
# (a)-(d) resolution
# --------------------------------------------------------------------------- #
def test_shipped_default_is_pause():
    """The whole point of the change: an add-on with no `deadline_passed_mode`
    in its config at all still pauses."""
    _setup()
    assert dl.DEFAULT_PASSED_MODE == dl.MODE_PAUSE
    assert dl.default_mode() == dl.MODE_PAUSE
    assert dl.entry_mode({"date": PAST}) == dl.MODE_PAUSE


def test_explicit_maintain_wins_over_pause_default():
    """(a) A deck that was asked and said "keep reviewing" keeps reviewing,
    however the config default is set."""
    _setup(config_mode=dl.MODE_PAUSE, state={"101": _entry(PAST, dl.MODE_MAINTAIN)})
    assert dl.entry_mode(_entry(PAST, dl.MODE_MAINTAIN)) == dl.MODE_MAINTAIN
    assert dl.get_mode(101) == dl.MODE_MAINTAIN
    assert dl.paused_deck_ids() == []


def test_explicit_pause_wins_over_maintain_default():
    """(b) And the mirror image: an explicit pause survives a "maintain"
    config default."""
    _setup(config_mode=dl.MODE_MAINTAIN, state={"101": _entry(PAST, dl.MODE_PAUSE)})
    assert dl.entry_mode(_entry(PAST, dl.MODE_PAUSE)) == dl.MODE_PAUSE
    assert dl.get_mode(101) == dl.MODE_PAUSE
    assert dl.paused_deck_ids() == [101]


def test_missing_mode_key_falls_back_to_config():
    """(c) The three decks in the real state dict with no `mode` key at all —
    they must follow the config, not a hardcoded maintain."""
    no_mode = _entry(PAST)
    assert "mode" not in no_mode

    _setup(config_mode=dl.MODE_PAUSE, state={"101": no_mode})
    assert dl.get_mode(101) == dl.MODE_PAUSE
    assert dl.paused_deck_ids() == [101]

    _setup(config_mode=dl.MODE_MAINTAIN, state={"101": no_mode})
    assert dl.get_mode(101) == dl.MODE_MAINTAIN
    assert dl.paused_deck_ids() == []


def test_config_maintain_restores_old_behavior():
    """(d) One key flips the whole feature back to how it shipped: every
    deck that never chose behaves exactly as the old hardcoded fallback did."""
    state = {
        "101": _entry(PAST),                      # implicit
        "102": _entry(PAST, dl.MODE_MAINTAIN),    # explicit maintain
        "103": _entry(PAST, dl.MODE_PAUSE),       # explicit pause
    }
    _setup(config_mode=dl.MODE_MAINTAIN, state=state)
    assert dl.get_mode(101) == dl.MODE_MAINTAIN
    assert dl.get_mode(102) == dl.MODE_MAINTAIN
    assert dl.get_mode(103) == dl.MODE_PAUSE
    # Only the deck that actually asked to be paused is paused.
    assert dl.paused_deck_ids() == [103]


def test_garbage_config_value_reads_as_the_default_not_an_error():
    for junk in ("Pause", "", "off", 1, None, [], {"mode": "pause"}):
        _setup(config_mode=junk)
        assert dl.default_mode() == dl.DEFAULT_PASSED_MODE, junk
    # Same for a junk value on the entry itself.
    _setup(config_mode=dl.MODE_PAUSE)
    assert dl.entry_mode({"date": PAST, "mode": "paws"}) == dl.MODE_PAUSE
    assert dl.entry_mode(None) == dl.MODE_PAUSE


def test_future_deadline_is_never_paused():
    """A pause only ever applies to a deadline that is actually behind us —
    `paused_deck_ids` feeds the sidebar's cross-collection queues, and
    subtracting a deck whose deadline is still ahead would hide work the
    user is meant to be doing."""
    _setup(config_mode=dl.MODE_PAUSE, state={
        "101": _entry(FUTURE),
        "102": _entry(FUTURE, dl.MODE_PAUSE),
        "103": _entry(PAST),
    })
    assert dl.paused_deck_ids() == [103]


def test_unparseable_entries_are_skipped_not_crashed_on():
    _setup(config_mode=dl.MODE_PAUSE, state={
        "101": {"date": "not-a-date"},
        "102": "not even a dict",
        "bogus": _entry(PAST),
        "103": _entry(PAST),
    })
    assert dl.paused_deck_ids() == [103]


# --------------------------------------------------------------------------- #
# (e) migration
# --------------------------------------------------------------------------- #
def test_implicit_maintain_predicate():
    """The eligibility rule, on its own: everything except an explicit
    pause is 'never chose anything but maintain'."""
    assert dl._is_implicit_maintain(_entry(PAST)) is True
    assert dl._is_implicit_maintain(_entry(PAST, dl.MODE_MAINTAIN)) is True
    assert dl._is_implicit_maintain(_entry(PAST, dl.MODE_PAUSE)) is False
    assert dl._is_implicit_maintain({"date": PAST, "mode": "nonsense"}) is True
    assert dl._is_implicit_maintain(None) is False


def _migration_fixture(config_mode=dl.MODE_PAUSE):
    state = {
        "101": _entry(PAST, cloned=9001),                    # no mode key
        "102": _entry(PAST, cloned=9002),                    # no mode key
        "103": _entry(PAST, cloned=9003),                    # no mode key
        "201": _entry(PAST, dl.MODE_MAINTAIN, cloned=9004),  # stale default
        "202": _entry(PAST, dl.MODE_MAINTAIN, cloned=9005),  # stale default
        "203": _entry(FUTURE, dl.MODE_MAINTAIN, cloned=9006),  # still ahead
    }
    return _setup(config_mode=config_mode, state=state)


def test_migration_pauses_only_passed_never_chosen_decks():
    """(e) The user's exact situation: three entries with no `mode`, two
    with the materialised "maintain" the old code wrote, one deadline still
    in the future."""
    col = _migration_fixture()
    paused = dl.migrate_passed_mode_default()
    assert paused == 5, f"expected 5 decks paused, got {paused}"

    for did in (101, 102, 103, 201, 202):
        assert _limits(col, did) == (0, 0), f"deck {did} not parked: {_limits(col, did)}"
        assert "mode" not in _state_now()[str(did)], (
            f"deck {did} should be left following the config, not pinned"
        )
        assert dl.get_mode(did) == dl.MODE_PAUSE

    # The future deadline is untouched — no limits written, mode intact.
    assert _limits(col, 203) == (None, None)
    assert _state_now()["203"]["mode"] == dl.MODE_MAINTAIN

    assert sorted(dl.paused_deck_ids()) == [101, 102, 103, 201, 202]


def test_migration_keeps_the_real_limits_it_parked_over():
    """Pausing snapshots whatever the deck had, so resuming gives it back —
    including a limit the user raised to hit the deadline."""
    col = _migration_fixture()
    col.decks.decks[101]["newLimit"] = 36
    dl.migrate_passed_mode_default()
    assert _limits(col, 101) == (0, 0)
    assert _state_now()["101"]["paused_limits"]["newLimit"] == 36


def test_migration_is_idempotent():
    col = _migration_fixture()
    dl.migrate_passed_mode_default()
    first = copy.deepcopy(_state_now())
    assert dl.migrate_passed_mode_default() == 0
    assert dl.migrate_passed_mode_default() == 0
    assert _state_now() == first
    assert _limits(col, 101) == (0, 0)


def test_migration_never_revisits_a_deliberate_later_maintain():
    """The guarantee the dated flag buys: a "maintain" the user picks *after*
    this shipped is written when the flag is already set, so no later run can
    flip it — even though it looks identical to the stale ones."""
    col = _migration_fixture()
    dl.migrate_passed_mode_default()

    # User reopens the dialog on a paused deck and picks "keep reviewing".
    dl.set_mode(101, dl.MODE_MAINTAIN)
    assert _state_now()["101"]["mode"] == dl.MODE_MAINTAIN
    assert _limits(col, 101) == (None, None), "resume should give the deck back"

    dl.migrate_passed_mode_default()
    assert _state_now()["101"]["mode"] == dl.MODE_MAINTAIN
    assert _limits(col, 101) == (None, None)
    assert 101 not in dl.paused_deck_ids()


def test_migration_with_config_maintain_pauses_nothing():
    """Config says maintain -> the migration still drops the stale key (so the
    decks follow the config from here) but parks no deck."""
    col = _migration_fixture(config_mode=dl.MODE_MAINTAIN)
    assert dl.migrate_passed_mode_default() == 0
    for did in (101, 201):
        assert _limits(col, did) == (None, None)
        assert "mode" not in _state_now()[str(did)]
        assert dl.get_mode(did) == dl.MODE_MAINTAIN
    assert dl.paused_deck_ids() == []


def test_migration_does_not_latch_without_a_collection():
    """No collection yet means nothing to apply — and the flag must stay
    unset so it runs properly on the next profile open, instead of being
    permanently skipped.

    (`enabled()` is the other bail-out, but it can't be exercised
    independently here: the feature toggle and the state dict share the
    `deck_deadlines` config key, so "off" and "no deadlines at all" are the
    same value.)"""
    _migration_fixture()
    _mw.col = None
    assert dl.migrate_passed_mode_default() == 0
    assert not _addons.cfg.get(dl._PASSED_MODE_MIGRATION_KEY)

    col = _migration_fixture()
    assert dl.migrate_passed_mode_default() == 5
    assert _addons.cfg.get(dl._PASSED_MODE_MIGRATION_KEY) is True
    assert _limits(col, 101) == (0, 0)


def test_set_deadline_no_longer_materialises_a_mode():
    """The root cause of the un-changeable default: every save used to write
    `mode: "maintain"` whether or not the user was ever asked."""
    col = _setup(config_mode=dl.MODE_PAUSE, state={})
    col.decks.decks[101]["conf"] = 1  # no clone yet; nothing else uses Default
    dl.set_deadline(101, datetime.date(2099, 1, 1))
    assert "mode" not in _state_now()["101"]

    # An answer the dialog actually collected is written, and wins.
    dl.set_deadline(101, datetime.date(2020, 1, 1), mode=dl.MODE_MAINTAIN)
    assert _state_now()["101"]["mode"] == dl.MODE_MAINTAIN
    assert dl.get_mode(101) == dl.MODE_MAINTAIN


# --------------------------------------------------------------------------- #
# Zeroed-clone repair
# --------------------------------------------------------------------------- #
def test_clone_source_name_parsing():
    assert dl._clone_source_name("Default — Amino Acids — deadline") == "Default"
    assert dl._clone_source_name("My preset — Week 1 — deadline") == "My preset"
    assert dl._clone_source_name("Default") is None
    assert dl._clone_source_name("Default — deadline") is None
    assert dl._clone_source_name(None) is None


def test_zeroed_clone_repair_unparks_a_stranded_preset():
    """A "— deadline" clone left at 0/day by the old preset-zeroing pause,
    with no state entry left to resume it, is restored from the preset it
    was cloned from — once."""
    col = _setup(config_mode=dl.MODE_PAUSE, state={})
    col.decks.confs[9001]["name"] = f"Default — Amino Acids{dl.CLONE_SUFFIX}"
    col.decks.confs[9001]["new"]["perDay"] = 0
    col.decks.confs[9001]["rev"]["perDay"] = 0

    assert dl.repair_zeroed_clone_presets() == 1
    assert col.decks.confs[9001]["new"]["perDay"] == 200
    assert col.decks.confs[9001]["rev"]["perDay"] == 2000
    # Never the interval ceiling — that is the deadline's business.
    assert col.decks.confs[9001]["rev"]["maxIvl"] == 36500
    # Once.
    col.decks.confs[9001]["new"]["perDay"] = 0
    assert dl.repair_zeroed_clone_presets() == 0
    assert col.decks.confs[9001]["new"]["perDay"] == 0


def test_zeroed_clone_repair_leaves_healthy_and_non_clone_presets_alone():
    col = _setup(config_mode=dl.MODE_PAUSE, state={})
    col.decks.confs[1]["new"]["perDay"] = 0  # a real preset, not ours to touch
    assert dl.repair_zeroed_clone_presets() == 0
    assert col.decks.confs[1]["new"]["perDay"] == 0



# --------------------------------------------------------------------------- #
# Cascade to subdecks
# --------------------------------------------------------------------------- #
def _tree():
    """Exam 1 with three subdecks (one nested two deep), all on Default."""
    col = _setup(config_mode=dl.MODE_PAUSE, state={})
    col.decks.decks.update({
        301: {"id": 301, "name": "Bio::Exam 1", "conf": 1},
        302: {"id": 302, "name": "Bio::Exam 1::01 pKa", "conf": 1},
        303: {"id": 303, "name": "Bio::Exam 1::02 Peptides", "conf": 1},
        304: {"id": 304, "name": "Bio::Exam 1::02 Peptides::Extra", "conf": 1},
        305: {"id": 305, "name": "Bio::Exam 10", "conf": 1},  # sibling: same prefix text, not a child
    })
    return col


def _max_ivl(col, did):
    return col.decks.config_dict_for_deck_id(did)["rev"]["maxIvl"]


def test_deadline_on_a_parent_reaches_every_subdeck():
    col = _tree()
    dl.set_deadline(301, datetime.date(2099, 1, 1), time_of_day=datetime.time(14, 0))
    st = _state_now()
    for did in (302, 303, 304):
        assert st[str(did)]["date"] == "2099-01-01", did
        assert st[str(did)]["time"] == "14:00"
        assert st[str(did)]["inherited_from"] == 301
        assert _max_ivl(col, did) < 36500, f"{did} ceiling not capped"
    assert "inherited_from" not in st["301"]
    assert "305" not in st, "sibling 'Exam 10' must not be treated as a child of 'Exam 1'"
    # Each subdeck got its own preset clone: Default is shared, so it is untouched.
    assert col.decks.confs[1]["rev"]["maxIvl"] == 36500


def test_subdecks_own_deadline_is_not_overwritten_by_the_parent():
    col = _tree()
    dl.set_deadline(303, datetime.date(2098, 6, 1))
    dl.set_deadline(301, datetime.date(2099, 1, 1))
    st = _state_now()
    assert st["303"]["date"] == "2098-06-01" and "inherited_from" not in st["303"]
    assert st["302"]["date"] == "2099-01-01"
    # Setting it directly on a subdeck later makes that deadline its own.
    dl.set_deadline(302, datetime.date(2097, 1, 1))
    assert "inherited_from" not in _state_now()["302"]


def test_mode_change_on_the_parent_follows_inherited_subdecks():
    col = _tree()
    dl.set_deadline(301, datetime.date(2020, 1, 1), mode=dl.MODE_PAUSE)
    assert _limits(col, 302) == (0, 0) and _limits(col, 304) == (0, 0)
    dl.set_mode(301, dl.MODE_MAINTAIN)
    st = _state_now()
    assert st["302"]["mode"] == dl.MODE_MAINTAIN
    assert _limits(col, 302) == (None, None), "resumed subdeck should have its limits back"


def test_clearing_the_parent_clears_inherited_subdecks_only():
    col = _tree()
    dl.set_deadline(303, datetime.date(2098, 6, 1))     # own
    dl.set_deadline(301, datetime.date(2099, 1, 1))     # cascades to 302, 304
    dl.clear_deadline(301)
    st = _state_now()
    assert "301" not in st and "302" not in st and "304" not in st
    assert st["303"]["date"] == "2098-06-01"
    assert _max_ivl(col, 302) == 36500 and _max_ivl(col, 304) == 36500


def test_daily_refresh_gives_new_subdecks_the_parents_deadline():
    col = _tree()
    dl.set_deadline(301, datetime.date(2099, 1, 1))
    col.decks.decks[306] = {"id": 306, "name": "Bio::Exam 1::09 Practice", "conf": 1}
    dl.refresh_all(force=True)
    st = _state_now()
    assert st["306"]["date"] == "2099-01-01" and st["306"]["inherited_from"] == 301
    assert _max_ivl(col, 306) < 36500


def test_raising_the_parents_limit_follows_inherited_subdecks():
    col = _tree()
    dl.set_deadline(301, datetime.date(2099, 1, 1))
    assert dl.set_new_per_day(301, 36)
    assert col.decks.decks[302]["newLimit"] == 36
    assert col.decks.decks[304]["newLimit"] == 36
    assert col.decks.decks[305].get("newLimit") is None

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
    print(f"PASS test_deadline_modes ({len(tests)} tests)")
