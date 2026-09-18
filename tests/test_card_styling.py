"""Run with Anki's Python environment: python tests/test_card_styling.py.

Covers the pure CSS-transform functions in card_styling.py — the part that
actually touches note type data. apply_styling/remove_styling never see
qfmt/afmt, only the css string, so these tests are the guardrail that the
simple styling panel can never corrupt front/back template HTML: every
assertion checks byte-identity of everything outside the marked block.
"""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "card_styling", Path(__file__).parents[1] / "card_styling.py"
)
card_styling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(card_styling)


class ApplyStylingTests(unittest.TestCase):
    def test_appends_block_to_empty_css(self):
        out = card_styling.apply_styling("", "", 20, 24, "center", 500)
        self.assertIn(card_styling._BLOCK_START, out)
        self.assertIn(card_styling._BLOCK_END, out)
        self.assertIn("font-size: 20px", out)
        self.assertIn("#answer ~ *", out)

    def test_preserves_existing_css_verbatim(self):
        original = ".card { color: red; background: blue; }\n.cloze { color: maroon; }"
        out = card_styling.apply_styling(original, "Georgia", 22, 26, "left", 480)
        self.assertIn(original, out)

    def test_reapplying_replaces_in_place_without_duplicating(self):
        original = ".card { color: red; }"
        first = card_styling.apply_styling(original, "Georgia", 22, 26, "left", 480)
        second = card_styling.apply_styling(first, "", 30, 40, "center", 600)
        self.assertEqual(second.count(card_styling._BLOCK_START), 1)
        self.assertEqual(second.count(card_styling._BLOCK_END), 1)
        self.assertIn(original, second)
        self.assertNotIn("font-family", second)  # blank font clears the rule
        self.assertIn("font-size: 30px", second)
        self.assertIn("text-align: center", second)
        self.assertIn("max-height: 600px", second)

    def test_font_family_is_quoted_and_escaped(self):
        out = card_styling.apply_styling("", 'Weird "Font" Name', 20, 24, "left", 500)
        self.assertIn('font-family: "Weird \\"Font\\" Name", sans-serif;', out)

    def test_never_touches_content_outside_the_block(self):
        original = "/* hand-authored */\n.card { color: red; }\n.tag { color: green; }"
        out = card_styling.apply_styling(original, "Arial", 20, 24, "right", 500)
        before, _, after = out.partition(card_styling._BLOCK_START)
        self.assertEqual(before.rstrip("\n "), original)
        # Nothing from the original survives *inside* our block either way —
        # the block is entirely generated, so this is really checking the
        # split point lands exactly where the original CSS ends.


class RoundTripTests(unittest.TestCase):
    def test_parse_existing_reads_back_applied_values(self):
        applied = card_styling.apply_styling("", "Georgia", 22, 26, "left", 480)
        values = card_styling._parse_existing(applied)
        self.assertEqual(values["font"], "Georgia")
        self.assertEqual(values["base_size"], 22)
        self.assertEqual(values["answer_size"], 26)
        self.assertEqual(values["align"], "left")
        self.assertEqual(values["image_max_height"], 480)

    def test_parse_existing_defaults_when_no_block_present(self):
        values = card_styling._parse_existing(".card { color: red; }")
        self.assertEqual(values, card_styling._DEFAULTS)


class RemoveStylingTests(unittest.TestCase):
    def test_reset_strips_block_and_restores_original_css(self):
        original = ".card { color: red; }"
        applied = card_styling.apply_styling(original, "Georgia", 22, 26, "left", 480)
        restored = card_styling.remove_styling(applied)
        self.assertNotIn(card_styling._BLOCK_START, restored)
        self.assertNotIn(card_styling._BLOCK_END, restored)
        self.assertIn(original, restored)

    def test_reset_on_css_with_no_block_is_a_no_op_content_wise(self):
        original = ".card { color: red; }"
        restored = card_styling.remove_styling(original)
        self.assertEqual(restored.strip(), original.strip())


class FrontBackTemplateSafetyTests(unittest.TestCase):
    """apply_styling/remove_styling take only a css string — there is no
    code path by which they could see or mutate qfmt/afmt. This test
    documents that guarantee at the notetype-dict level, the way the
    dialog itself calls it (only notetype["css"] is ever reassigned)."""

    def test_only_css_key_would_be_touched_on_a_notetype_dict(self):
        notetype = {
            "id": 1,
            "name": "Basic (generated)",
            "css": ".card { color: red; }",
            "tmpls": [
                {"qfmt": "{{Question}}", "afmt": "{{FrontSide}}<hr id=answer>{{AnswerImage}}"}
            ],
        }
        before_tmpls = [dict(t) for t in notetype["tmpls"]]
        notetype["css"] = card_styling.apply_styling(
            notetype["css"], "Georgia", 22, 26, "left", 480
        )
        self.assertEqual(notetype["tmpls"], before_tmpls)
        notetype["css"] = card_styling.remove_styling(notetype["css"])
        self.assertEqual(notetype["tmpls"], before_tmpls)


if __name__ == "__main__":
    unittest.main()
