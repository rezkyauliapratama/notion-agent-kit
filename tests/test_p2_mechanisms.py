"""Regression tests for the P2 mechanism package (production rules 2026-10-05).

Covers:
1. `after` parameter: only the first batch carries it; the local parent-sibling
   guard rejects a mismatched block and returns NOT_FOUND for a missing one.
2. notion_duplicate_page: signature read-back verification and copyable
   properties (read-only skipped).
3. notion_update_table_row: per-cell annotation preservation (rule 6).
4. Table guard: a table without inline children is rejected locally (rule 3).
5. notion_replace_table: create-after then delete-old ordering (rule 5).
6. Append validation: a response with MORE results than blocks sent is not
   treated as a failure (rule 4).

All tests are offline and use fake clients; no Notion API call is made.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.block_batcher import BlockBatcher
from core.block_converter import MarkdownConverter
from handlers.write_handler import WriteHandler, block_signature


def run(coro):
    return asyncio.run(coro)


def para(text):
    return {"type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


# ---------------------------------------------------------------------------
# 1. `after` batching + local guard
# ---------------------------------------------------------------------------

class RecordingAfterClient:
    """Records the `after` value seen by each append call."""

    def __init__(self):
        self.calls = []

    async def append_block_children(self, parent_block_id, children, after=None):
        self.calls.append({"parent": parent_block_id, "n": len(children), "after": after})
        return {"results": children}


class TestAfterBatching(unittest.TestCase):
    def test_after_only_on_first_batch(self):
        client = RecordingAfterClient()
        batcher = BlockBatcher(client=client)
        blocks = [{"type": "paragraph", "id": i} for i in range(101)]
        run(batcher.append_blocks("parent", blocks, after="b0"))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["after"], "b0")
        self.assertIsNone(client.calls[1]["after"])

    def test_no_after_is_backward_compatible(self):
        client = RecordingAfterClient()
        batcher = BlockBatcher(client=client)
        run(batcher.append_blocks("parent", [{"type": "paragraph", "id": 1}]))
        self.assertIsNone(client.calls[0]["after"])


class AfterGuardClient:
    """Fake client exposing retrieve_block + append + read-back."""

    def __init__(self, after_block):
        self.after_block = after_block
        self.appended = []
        self.appended_after = None

    async def retrieve_block(self, block_id):
        return self.after_block

    async def append_block_children(self, parent_id, children, after=None):
        self.appended.append(list(children))
        self.appended_after = after
        return {"results": children}

    async def retrieve_block_children(self, parent_id, page_size=100, start_cursor=None):
        blocks = self.appended[-1] if self.appended else []
        return {"results": blocks, "has_more": False}


def make_handler(client):
    return WriteHandler(client=client, converter=MarkdownConverter(),
                        batcher=BlockBatcher(client=client))


class TestAfterLocalGuard(unittest.TestCase):
    def test_parent_mismatch_rejected(self):
        after = {"id": "h1", "type": "heading_2", "has_children": True,
                 "parent": {"type": "page_id", "page_id": "otherpage"}}
        client = AfterGuardClient(after)
        handler = make_handler(client)
        result = run(handler.write_blocks("mypage", [para("x")], after="h1"))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(client.appended, [])

    def test_missing_after_returns_not_found(self):
        client = AfterGuardClient({"error": True, "code": "NOT_FOUND",
                                   "message": "no such block"})
        handler = make_handler(client)
        result = run(handler.write_blocks("mypage", [para("x")], after="ghost"))
        self.assertEqual(result["code"], "NOT_FOUND")
        self.assertEqual(client.appended, [])

    def test_leaf_after_accepted_and_forwarded(self):
        after = {"id": "h1", "type": "paragraph", "has_children": False,
                 "parent": {"type": "page_id", "page_id": "otherpage"}}
        client = AfterGuardClient(after)
        handler = make_handler(client)
        result = run(handler.write_blocks("mypage", [para("x")], after="h1"))
        self.assertNotIn("error", result)
        self.assertEqual(client.appended_after, "h1")
        self.assertIsNone(result["verified"])
        self.assertIn("after", result["explain"])

    def test_after_same_parent_accepted(self):
        after = {"id": "h1", "type": "heading_2", "has_children": True,
                 "parent": {"type": "page_id", "page_id": "mypage"}}
        client = AfterGuardClient(after)
        handler = make_handler(client)
        result = run(handler.write_blocks("mypage", [para("x")], after="h1"))
        self.assertNotIn("error", result)
        self.assertEqual(client.appended_after, "h1")


# ---------------------------------------------------------------------------
# 1b. `after` disables tail verification (no false mismatch) + guards
# ---------------------------------------------------------------------------

class AfterClientWithAppend(AfterGuardClient):
    """Extends the after-guard client so append_to_page can also be driven."""


class TestAfterSkipsTailVerification(unittest.TestCase):
    def test_append_with_after_skips_verification(self):
        # Blocks land mid-page under `after`; the tail read-back would always
        # mismatch, so verification must be skipped (None), not False.
        after = {"id": "h1", "type": "paragraph", "has_children": False,
                 "parent": {"type": "page_id", "page_id": "otherpage"}}
        client = AfterClientWithAppend(after)
        handler = make_handler(client)
        result = run(handler.append_to_page("mypage", "SISIP1", after="h1"))
        self.assertNotIn("error", result)
        self.assertIsNone(result["verified"])
        self.assertIn("after", result["explain"])
        self.assertEqual(client.appended_after, "h1")

    def test_missing_after_id_returns_not_found(self):
        client = AfterClientWithAppend(
            {"error": True, "code": "NOT_FOUND", "message": "no such block"})
        handler = make_handler(client)
        result = run(handler.append_to_page("mypage", "SISIP1", after="ghost"))
        self.assertEqual(result["code"], "NOT_FOUND")
        self.assertNotIn("verified", result)
        self.assertEqual(client.appended, [])

    def test_after_parent_mismatch_rejected_for_append(self):
        after = {"id": "h1", "type": "heading_2", "has_children": True,
                 "parent": {"type": "page_id", "page_id": "otherpage"}}
        client = AfterClientWithAppend(after)
        handler = make_handler(client)
        result = run(handler.append_to_page("mypage", "SISIP1", after="h1"))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(client.appended, [])


# ---------------------------------------------------------------------------
# 2. duplicate_page
# ---------------------------------------------------------------------------

class DuplicateClient:
    """In-memory fake that models a source page and the created copy."""

    def __init__(self, mutate_readback=False):
        self.tree = {
            "src": [
                {"id": "s1", "type": "paragraph",
                 "paragraph": {"rich_text": [{"plain_text": "Hello",
                                              "annotations": {"bold": False}}]}},
                {"id": "s2", "type": "heading_2",
                 "heading_2": {"rich_text": [{"plain_text": "Title",
                                              "annotations": {"bold": False}}]}},
            ],
        }
        self.created = None
        self.mutate_readback = mutate_readback

    async def retrieve_page(self, page_id):
        return {
            "id": page_id, "object": "page", "url": "https://notion.so/src",
            "parent": {"type": "database_id", "database_id": "db1"},
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "My Page"}]},
                "Count": {"type": "number", "number": 5},
                "Kind": {"type": "select", "select": {"name": "Food"}},
                "Total": {"type": "formula", "formula": {"number": 99}},
                "Note": {"type": "rich_text", "rich_text": [{"plain_text": "hi"}]},
            },
        }

    async def retrieve_block_children(self, parent_id, page_size=100, start_cursor=None):
        results = list(self.tree.get(parent_id, []))
        if self.mutate_readback and parent_id == "newpage":
            results = [{"id": "x", "type": "paragraph",
                        "paragraph": {"rich_text": [{"plain_text": "WRONG"}]}}]
        return {"results": results, "has_more": False}

    async def create_page(self, parent, properties, children=None):
        self.created = {"parent": parent, "properties": properties}
        return {"id": "newpage", "url": "https://notion.so/newpage", "object": "page"}

    async def append_block_children(self, parent_id, children, after=None):
        made = []
        for i, child in enumerate(children):
            block = dict(child)
            block["id"] = f"new-{i}"
            made.append(block)
        self.tree.setdefault(parent_id, []).extend(made)
        return {"results": made}


class TestDuplicatePage(unittest.TestCase):
    def test_duplicate_verified_signatures(self):
        client = DuplicateClient()
        handler = WriteHandler(client=client, converter=None, batcher=None)
        result = run(handler.duplicate_page("src"))
        self.assertNotIn("error", result)
        self.assertEqual(result["page_id"], "newpage")
        self.assertTrue(result["verified"])
        self.assertEqual(result["blocks_copied"], 2)

    def test_duplicate_signature_mismatch_keeps_page(self):
        client = DuplicateClient(mutate_readback=True)
        handler = WriteHandler(client=client, converter=None, batcher=None)
        result = run(handler.duplicate_page("src"))
        self.assertNotIn("error", result)
        self.assertEqual(result["page_id"], "newpage")
        self.assertFalse(result["verified"])
        self.assertIn("explain", result)

    def test_duplicate_copies_properties_skips_readonly(self):
        client = DuplicateClient()
        handler = WriteHandler(client=client, converter=None, batcher=None)
        run(handler.duplicate_page("src"))
        props = client.created["properties"]
        self.assertEqual(props["Name"]["title"][0]["text"]["content"], "My Page (copy)")
        self.assertEqual(props["Count"], {"number": 5})
        self.assertEqual(props["Kind"], {"select": {"name": "Food"}})
        self.assertNotIn("Total", props)  # formula is read-only

    def test_duplicate_title_override(self):
        client = DuplicateClient()
        handler = WriteHandler(client=client, converter=None, batcher=None)
        run(handler.duplicate_page("src", title="Backup"))
        self.assertEqual(client.created["properties"]["Name"]["title"][0]["text"]["content"],
                         "Backup")


# ---------------------------------------------------------------------------
# 3. update_table_row
# ---------------------------------------------------------------------------

def cell(text, bold=False):
    return [{"type": "text", "text": {"content": text},
             "annotations": {"bold": bold, "italic": False, "strikethrough": False,
                             "underline": False, "code": False, "color": "default"}}]


class TableRowClient:
    def __init__(self, block):
        self.block = block
        self.updated = None

    async def retrieve_block(self, block_id):
        return self.block

    async def update_block(self, block_id, data):
        self.updated = data
        return {"id": block_id, **data}


class TestUpdateTableRow(unittest.TestCase):
    def test_string_cell_preserves_old_annotations(self):
        block = {"id": "r1", "type": "table_row",
                 "table_row": {"cells": [cell("old", bold=True), cell("second")]}}
        client = TableRowClient(block)
        handler = WriteHandler(client=client, converter=None, batcher=None)
        result = run(handler.update_table_row("r1", ["New", "X"]))
        self.assertEqual(result["type"], "table_row")
        self.assertEqual(result["cells"], 2)
        new_cells = client.updated["table_row"]["cells"]
        self.assertEqual(new_cells[0][0]["text"]["content"], "New")
        self.assertTrue(new_cells[0][0]["annotations"]["bold"])
        self.assertFalse(new_cells[1][0]["annotations"]["bold"])

    def test_explicit_annotations_override(self):
        block = {"id": "r1", "type": "table_row",
                 "table_row": {"cells": [cell("old", bold=True)]}}
        client = TableRowClient(block)
        handler = WriteHandler(client=client, converter=None, batcher=None)
        run(handler.update_table_row("r1", [{"text": "X", "annotations": {"italic": True}}]))
        annotations = client.updated["table_row"]["cells"][0][0]["annotations"]
        self.assertTrue(annotations["italic"])
        self.assertFalse(annotations["bold"])

    def test_non_row_block_type_mismatch(self):
        block = {"id": "p1", "type": "paragraph",
                 "paragraph": {"rich_text": [{"plain_text": "x"}]}}
        client = TableRowClient(block)
        handler = WriteHandler(client=client, converter=None, batcher=None)
        result = run(handler.update_table_row("p1", ["a"]))
        self.assertEqual(result["code"], "BLOCK_TYPE_MISMATCH")


# ---------------------------------------------------------------------------
# 4. table without inline children is rejected locally
# ---------------------------------------------------------------------------

class TestTableGuard(unittest.TestCase):
    def test_table_without_children_rejected(self):
        client = AfterGuardClient({"id": "x", "type": "paragraph", "has_children": False})
        handler = make_handler(client)
        blocks = [{"type": "table", "table": {"table_width": 2}}]
        result = run(handler.write_blocks("mypage", blocks))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertIn("children", result["message"])
        self.assertEqual(client.appended, [])

    def test_table_with_inline_rows_accepted(self):
        client = AfterGuardClient({"id": "x", "type": "paragraph", "has_children": False})
        handler = make_handler(client)
        blocks = [{"type": "table", "table": {
            "table_width": 2, "has_column_header": True,
            "children": [{"type": "table_row", "table_row": {"cells": [cell("a"), cell("b")]}}]}}]
        result = run(handler.write_blocks("mypage", blocks))
        self.assertNotIn("error", result)

    def test_nested_table_without_children_rejected(self):
        client = AfterGuardClient({"id": "x", "type": "paragraph", "has_children": False})
        handler = make_handler(client)
        blocks = [{"type": "column_list", "column_list": {"children": [
            {"type": "column", "column": {"children": [
                {"type": "table", "table": {"table_width": 1}}]}}]}}]
        result = run(handler.write_blocks("mypage", blocks))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(client.appended, [])


# ---------------------------------------------------------------------------
# 5. replace_table ordering
# ---------------------------------------------------------------------------

class ReplaceTableClient:
    def __init__(self):
        self.calls = []

    async def retrieve_block(self, block_id):
        return {"id": block_id, "type": "table", "has_children": True,
                "parent": {"type": "page_id", "page_id": "p1"},
                "table": {"table_width": 2, "has_column_header": True,
                          "has_row_header": False}}

    async def append_block_children(self, parent_id, children, after=None):
        self.calls.append(("append", parent_id, after, children))
        return {"results": [{"id": "newtable", "type": "table"}]}

    async def delete_block(self, block_id):
        self.calls.append(("delete", block_id))
        return {"id": block_id, "object": "block"}

    async def retrieve_block_children(self, parent_id, page_size=100, start_cursor=None):
        return {"results": [{"type": "table_row"}] * 3, "has_more": False}


class TestReplaceTable(unittest.TestCase):
    def test_replace_creates_after_then_deletes(self):
        client = ReplaceTableClient()
        handler = WriteHandler(client=client, converter=None, batcher=None)
        result = run(handler.replace_table("oldtable", [["a", "b", "c"], ["d", "e", "f"]]))
        kinds = [c[0] for c in client.calls]
        self.assertEqual(kinds, ["append", "delete"])
        append_call = client.calls[0]
        self.assertEqual(append_call[1], "p1")
        self.assertEqual(append_call[2], "oldtable")  # after == old table id
        self.assertEqual(result["new_table_id"], "newtable")
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["columns"], 3)

    def test_ragged_rows_rejected(self):
        client = ReplaceTableClient()
        handler = WriteHandler(client=client, converter=None, batcher=None)
        result = run(handler.replace_table("oldtable", [["a", "b"], ["c"]]))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(client.calls, [])


# ---------------------------------------------------------------------------
# 6. append validation tolerates extra results (rule 4)
# ---------------------------------------------------------------------------

class AppendClient:
    """Append returns MORE results than blocks sent, plus a controlled read-back."""

    def __init__(self, read_back=None, extra_results=0):
        self.sent = []
        self.extra_results = extra_results
        self.read_back = read_back

    async def append_block_children(self, parent_id, children, after=None):
        self.sent.append(list(children))
        results = [dict(c) for c in children]
        for k in range(self.extra_results):
            results.append({"type": "paragraph", "id": f"extra{k}",
                            "paragraph": {"rich_text": [{"plain_text": "EXTRA"}]}})
        return {"results": results}

    async def retrieve_block_children(self, parent_id, page_size=100, start_cursor=None):
        if self.read_back is not None:
            return {"results": self.read_back, "has_more": False}
        blocks = [b for chunk in self.sent for b in chunk]
        return {"results": blocks, "has_more": False}


class TestAppendVerification(unittest.TestCase):
    def test_extra_results_not_a_failure(self):
        client = AppendClient(extra_results=2)
        handler = WriteHandler(client=client, converter=MarkdownConverter(),
                               batcher=BlockBatcher(client=client))
        result = run(handler.append_to_page("page1", "Hello world"))
        self.assertNotIn("error", result)
        self.assertTrue(result["verified"])
        # Older keys remain for backward compatibility.
        self.assertEqual(result["block_count"], 1)
        self.assertIn("batches", result)
        self.assertIn("duration_ms", result)

    def test_signature_mismatch_reports_false(self):
        wrong = [{"type": "paragraph",
                  "paragraph": {"rich_text": [{"plain_text": "WRONG"}]}}]
        client = AppendClient(read_back=wrong)
        handler = WriteHandler(client=client, converter=MarkdownConverter(),
                               batcher=BlockBatcher(client=client))
        result = run(handler.append_to_page("page1", "Hello world"))
        self.assertFalse(result["verified"])
        self.assertIn("explain", result)
        self.assertNotIn("error", result)

    def test_multi_batch_skips_verification_with_note(self):
        client = AppendClient()
        handler = WriteHandler(client=client, converter=MarkdownConverter(),
                               batcher=BlockBatcher(client=client))
        markdown = "\n\n".join(f"line {i}" for i in range(101))
        result = run(handler.append_to_page("page1", markdown))
        self.assertEqual(result["batches"], 2)
        self.assertIsNone(result["verified"])
        self.assertIn("skipped", result["explain"])


if __name__ == "__main__":
    unittest.main()
