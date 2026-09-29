"""Live: the sidebar's Due / New / Learning totals count every deck's cards,
even when a parent deck's own daily limits are lower than its children's.

Ticket 20260929-101708: Microbiology's own preset clipped its row to 0 new
and 0 due while its subdeck "Lab Exam 1 Content" listed 121 new and 2 due.
The sidebar summed only the top-level rows, so it read Due 0 / New 0.

Fixture: Parent::{A,B,C} and Solo, two new cards in each leaf. Parent gets
its own preset with 0 new / 0 reviews a day (children keep the default
preset), and A's two cards are made due reviews today. The deck list then
shows Parent 0 / 0 while A has 2 due and B, C have 2 new each; the sidebar
must read Due 2 and New 6 (B, C and Solo).
"""


def _sidebar(t):
    return t.js(
        "(function(){var o={};['due','new','learn'].forEach(function(k){"
        "var e=document.querySelector('.ba-side [data-x=\"'+k+'\"]');"
        "o[k]=e?e.textContent.trim():null;});return o;})()")


def run(t):
    mw = t.mw
    col = mw.col
    ok = t.wait_js("document.querySelectorAll('.ad-list-row').length > 0", timeout=20)
    t.check("deck list rendered", ok, "")

    parent = col.decks.by_name("Parent")
    a = col.decks.by_name("Parent::A")
    conf = col.decks.add_config_returning_id("Parent limits")
    c = col.decks.get_config(conf)
    c["new"]["perDay"] = 0
    c["rev"]["perDay"] = 0
    col.decks.update_config(c)
    parent["conf"] = conf
    col.decks.save(parent)

    a_cids = list(col.find_cards(f"did:{a['id']}"))
    col.sched.set_due_date(a_cids, "0")

    tree = col.sched.deck_due_tree()
    counts = {}

    def walk(node):
        counts[node.name] = (node.new_count, node.learn_count, node.review_count)
        for ch in node.children:
            walk(ch)
    for ch in tree.children:
        walk(ch)
    t.note(f"deck tree counts (new, learn, due): {counts}")
    t.check("Parent's own row is clipped to 0 new / 0 due",
            counts.get("Parent", (None, None, None))[0] == 0
            and counts.get("Parent", (None, None, None))[2] == 0, counts)

    mw.deckBrowser.refresh()
    t.pump(1500)
    t.wait_js("document.querySelector('.ba-side [data-x=\"due\"]') !== null", timeout=10)
    t.pump(500)
    s = _sidebar(t)
    t.check("sidebar Due counts A's 2 due reviews", s.get("due") == "2", s)
    t.check("sidebar New counts B, C and Solo's 6 new cards", s.get("new") == "6", s)
    t.check("sidebar Learning reads 0", s.get("learn") == "0", s)
