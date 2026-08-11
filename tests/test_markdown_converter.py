"""Tests for MarkdownConverter inline formatting."""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.block_converter import MarkdownConverter


class TestInlineFormatting(unittest.TestCase):
    def setUp(self):
        self.converter = MarkdownConverter()

    def test_bold_at_start(self):
        """Bold at start of line still works."""
        rich = self.converter._parse_inline_formatting("**Governance anchor** — prinsip")
        self.assertEqual(rich[0]["annotations"]["bold"], True)
        self.assertEqual(rich[0]["text"]["content"], "Governance anchor")

    def test_bold_mid_sentence(self):
        """Bold in the middle of a sentence must be parsed (regression fix)."""
        rich = self.converter._parse_inline_formatting(
            "Dokumen ini menjadi **anchor tata kelola** arsitektur"
        )
        segments = [(r["annotations"]["bold"], r["text"]["content"]) for r in rich]
        self.assertEqual(segments, [
            (False, "Dokumen ini menjadi "),
            (True, "anchor tata kelola"),
            (False, " arsitektur"),
        ])
        # No literal asterisks should remain
        all_text = "".join(r["text"]["content"] for r in rich)
        self.assertNotIn("**", all_text)

    def test_mixed_italic_and_bold(self):
        """Italic and bold mixed mid-sentence."""
        rich = self.converter._parse_inline_formatting(
            "teks *miring* dan **tebal** bareng"
        )
        segments = [
            (r["annotations"]["bold"], r["annotations"]["italic"], r["text"]["content"])
            for r in rich
        ]
        self.assertEqual(segments, [
            (False, False, "teks "),
            (False, True, "miring"),
            (False, False, " dan "),
            (True, False, "tebal"),
            (False, False, " bareng"),
        ])

    def test_link_mid_sentence(self):
        """Markdown link mid-sentence."""
        rich = self.converter._parse_inline_formatting("lihat [link](https://x.com) disini")
        link_segments = [r["text"].get("link") for r in rich]
        self.assertEqual(link_segments[1]["url"], "https://x.com")

    def test_inline_code_mid_sentence(self):
        """Inline code mid-sentence."""
        rich = self.converter._parse_inline_formatting("pakai `kode` ya")
        segments = [(r["annotations"]["code"], r["text"]["content"]) for r in rich]
        self.assertEqual(segments, [
            (False, "pakai "),
            (True, "kode"),
            (False, " ya"),
        ])

    def test_plain_text(self):
        """Plain text without formatting is unchanged."""
        rich = self.converter._parse_inline_formatting("plain text saja")
        self.assertEqual(len(rich), 1)
        self.assertEqual(rich[0]["text"]["content"], "plain text saja")

    def test_multiple_bold_segments(self):
        """Two bold segments in one line."""
        rich = self.converter._parse_inline_formatting(
            "**A** dan **B** adalah prinsip"
        )
        bold_segments = [
            r["text"]["content"] for r in rich if r["annotations"]["bold"]
        ]
        self.assertEqual(bold_segments, ["A", "B"])

    def test_empty_text(self):
        """Empty input returns a single empty rich text."""
        rich = self.converter._parse_inline_formatting("")
        self.assertEqual(len(rich), 1)
        self.assertEqual(rich[0]["text"]["content"], "")


if __name__ == "__main__":
    unittest.main()
