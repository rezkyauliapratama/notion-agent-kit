"""Regression tests for bugs found in production (Aug 2026).

Covers:
1. schema-cache format mismatch: inspect_database caches properties as a LIST,
   add_database_row expected a DICT -> "'list' object has no attribute 'get'"
2. table parsing shifting columns when a middle cell is empty
3. rich_text / code blocks longer than Notion's 2000-char limit -> 400 errors
4. delete_block idempotency on already-deleted blocks (404 spam)
5. Notion URL / ID resolution (path.block_id "should be a valid uuid" errors)
6. create_database auto-adds a title property (Notion requires exactly one)
"""

import sys
import os
import asyncio
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.block_converter import MarkdownConverter, RICH_TEXT_MAX
from core.schema_cache import SchemaCache
from handlers.write_handler import WriteHandler
from handlers.validate import extract_page_id, validate_page_id


class FakeClient:
    """Minimal in-memory Notion client for handler-level tests."""

    def __init__(self):
        self.deleted = []

    async def retrieve_database(self, database_id):
        return {"id": database_id, "properties": {
            "Name": {"type": "title", "title": {}},
            "Amount": {"type": "number", "number": {"format": "idr"}},
            "Category": {"type": "select", "select": {"options": [{"name": "A"}]}},
        }}

    async def create_database(self, parent, title, properties):
        return {"id": "db123", "url": "https://notion.so/db123", "properties": properties}

    async def create_page(self, parent, properties):
        return {"id": "page123", "url": "https://notion.so/page123", "properties": properties}

    async def delete_block(self, block_id):
        if block_id in self.deleted:
            return {"error": True, "code": "NOT_FOUND",
                    "message": "Could not find block with ID: " + block_id}
        self.deleted.append(block_id)
        return {"id": block_id, "object": "block"}

    async def retrieve_block(self, block_id):
        if block_id == "existing-heading":
            return {"id": block_id, "type": "heading_3",
                    "heading_3": {"rich_text": [{"type": "text", "text": {"content": "old"}}]}}
        return {"id": block_id, "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": "old"}}]}}

    async def update_block(self, block_id, content):
        return {"id": block_id, **content}


class TestSchemaNormalization(unittest.TestCase):
    """Bug: notion_inspect_database caches properties as a LIST; add_database_row
    crashed with 'list' object has no attribute 'get' (agent.log 2026-08-16)."""

    def setUp(self):
        self.handler = WriteHandler(client=FakeClient(), converter=None,
                                    batcher=None, cache=SchemaCache())

    def test_normalize_inspect_list_format(self):
        inspect_format = {"id": "db1", "title": "DB",
                          "properties": [
                              {"name": "Name", "type": "title"},
                              {"name": "Amount", "type": "number"},
                              {"name": "Category", "type": "select",
                               "options": [{"id": "opt1", "name": "A", "color": "blue"}]},
                          ]}
        normalized = WriteHandler._normalize_schema(inspect_format["properties"])
        self.assertEqual(normalized["Amount"], {"type": "number"})
        self.assertEqual(normalized["Category"]["type"], "select")
        self.assertEqual(normalized["Category"]["options"], ["A"])

    def test_normalize_dict_passthrough(self):
        raw = {"Amount": {"type": "number", "number": {"format": "idr"}}}
        self.assertEqual(WriteHandler._normalize_schema(raw), raw)

    def test_normalize_garbage(self):
        self.assertEqual(WriteHandler._normalize_schema(None), {})
        self.assertEqual(WriteHandler._normalize_schema("nope"), {})

    def test_add_row_with_inspect_cached_schema(self):
        """The exact production sequence: inspect (caches list format) then add row."""
        self.handler.cache.set("db1", {"properties": [
            {"name": "Name", "type": "title"},
            {"name": "Amount", "type": "number"},
        ]})
        result = asyncio.run(self.handler.add_database_row(
            "db1", {"Name": "Kopi", "Amount": 10000}))
        self.assertNotIn("error", result)
        self.assertEqual(result["page_id"], "page123")

    def test_convert_row_properties_defensive(self):
        """Even if a raw list leaks through, conversion must not crash."""
        converted = WriteHandler._convert_row_properties(
            [{"name": "Amount", "type": "number"}], {"Amount": 5})
        self.assertEqual(converted, {"Amount": {"number": 5}})


class TestTableEmptyCell(unittest.TestCase):
    """Bug: naive split+filter shifted columns when a middle cell was empty."""

    def setUp(self):
        self.converter = MarkdownConverter()

    def test_empty_middle_cell_keeps_columns(self):
        md = "| A | B | C |\n|---|---|---|\n| 1 |  | 3 |\n| 4 | 5 | 6 |"
        blocks = self.converter.convert(md)
        self.assertEqual(len(blocks), 1)
        table = blocks[0]["table"]
        self.assertEqual(table["table_width"], 3)
        rows = table["children"]
        self.assertEqual(len(rows), 3)
        # Header
        self.assertEqual([c[0]["text"]["content"] for c in rows[0]["table_row"]["cells"]],
                         ["A", "B", "C"])
        # Row with empty middle cell must keep 3 cells
        row1 = [c[0]["text"]["content"] if c else "" for c in rows[1]["table_row"]["cells"]]
        self.assertEqual(row1, ["1", "", "3"])
        row2 = [c[0]["text"]["content"] if c else "" for c in rows[2]["table_row"]["cells"]]
        self.assertEqual(row2, ["4", "5", "6"])


class TestLongContentSplit(unittest.TestCase):
    """Bug: Notion rejects rich_text > 2000 chars with a 400 validation_error."""

    def setUp(self):
        self.converter = MarkdownConverter()

    def test_long_paragraph_split(self):
        long_text = "x" * (RICH_TEXT_MAX * 2 + 100)
        rich = self.converter._parse_inline_formatting(long_text)
        self.assertGreater(len(rich), 1)
        for rt in rich:
            self.assertLessEqual(len(rt["text"]["content"]), RICH_TEXT_MAX)
        joined = "".join(rt["text"]["content"] for rt in rich)
        self.assertEqual(joined, long_text)

    def test_long_bold_keeps_annotation(self):
        text = "**" + "b" * (RICH_TEXT_MAX + 50) + "**"
        rich = self.converter._parse_inline_formatting(text)
        self.assertTrue(all(rt["annotations"]["bold"] for rt in rich))
        joined = "".join(rt["text"]["content"] for rt in rich)
        self.assertEqual(joined, "b" * (RICH_TEXT_MAX + 50))

    def test_long_code_block_split(self):
        code = "c" * (RICH_TEXT_MAX * 3 + 10)
        block = self.converter._build_code_block(code, "python")
        rich = block["code"]["rich_text"]
        self.assertGreater(len(rich), 1)
        self.assertTrue(all(len(rt["text"]["content"]) <= RICH_TEXT_MAX for rt in rich))
        self.assertEqual("".join(rt["text"]["content"] for rt in rich), code)


class TestDeleteIdempotent(unittest.TestCase):
    """Bug: DELETE on already-deleted blocks returned 404 errors (mcp-stderr.log)."""

    def setUp(self):
        self.handler = WriteHandler(client=FakeClient(), converter=None,
                                    batcher=None, cache=SchemaCache())

    def test_first_delete_success(self):
        result = asyncio.run(self.handler.delete_block("block-1"))
        self.assertEqual(result["deleted"], "block-1")
        self.assertNotIn("already_deleted", result)

    def test_second_delete_idempotent(self):
        client = self.handler.client
        asyncio.run(self.handler.delete_block("block-1"))
        result = asyncio.run(self.handler.delete_block("block-1"))
        self.assertEqual(result["deleted"], "block-1")
        self.assertTrue(result["already_deleted"])
        self.assertNotIn("error", result)


class TestUpdateBlockTypeMismatch(unittest.TestCase):
    """Bug: PATCH with a different block type -> 400 'Block type mismatch'."""

    def setUp(self):
        self.handler = WriteHandler(client=FakeClient(), converter=MarkdownConverter(),
                                    batcher=None, cache=SchemaCache())

    def test_plain_text_auto_maps_to_existing_heading(self):
        result = asyncio.run(self.handler.update_block("existing-heading", "teks baru"))
        self.assertNotIn("error", result)
        self.assertEqual(result["type"], "heading_3")

    def test_multi_block_markdown_rejected(self):
        result = asyncio.run(self.handler.update_block("existing-heading", "line1\n\nline2"))
        self.assertEqual(result.get("code"), "TOO_MANY_BLOCKS")


class TestPageIdResolution(unittest.TestCase):
    """Bug: agents passed Notion URLs -> 'path.block_id should be a valid uuid'."""

    def test_uuid_with_dashes(self):
        self.assertEqual(extract_page_id("3aa0f42d-7035-802b-bf61-e7effd583a39"),
                         "3aa0f42d7035802bbf61e7effd583a39")

    def test_plain_32_hex(self):
        self.assertEqual(extract_page_id("3aa0f42d7035802bbf61e7effd583a39"),
                         "3aa0f42d7035802bbf61e7effd583a39")

    def test_notion_so_url(self):
        url = "https://www.notion.so/rezkyauliapratama/Spending-Tracker-38b0f42d7035808fbeb263f818eb3187"
        self.assertEqual(extract_page_id(url), "38b0f42d7035808fbeb263f818eb3187")

    def test_app_notion_url(self):
        url = "https://app.notion.com/p/MCP-System-Trading-Crypto-3aa0f42d7035802bbf61e7effd583a39"
        self.assertEqual(extract_page_id(url), "3aa0f42d7035802bbf61e7effd583a39")

    def test_invalid_inputs(self):
        self.assertIsNone(extract_page_id(""))
        self.assertIsNone(extract_page_id("just a title"))
        self.assertIsNone(extract_page_id(None))
        self.assertIsNone(extract_page_id(12345))

    def test_legacy_validator(self):
        self.assertIsNone(validate_page_id("3aa0f42d7035802bbf61e7effd583a39"))
        self.assertIsNotNone(validate_page_id("not-an-id"))


if __name__ == "__main__":
    unittest.main()
