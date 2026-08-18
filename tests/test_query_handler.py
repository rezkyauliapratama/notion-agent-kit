"""Tests for QueryHandler (notion_query_database tool)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.query_handler import QueryHandler


class FakeClient:
    """Fake Notion client returning a canned query response."""

    async def query_database(self, database_id, filter_obj=None, sorts=None, page_size=100):
        return {
            "results": [
                {
                    "id": "row1", "url": "https://notion.so/row1",
                    "created_time": "2026-08-01T00:00:00.000Z",
                    "last_edited_time": "2026-08-02T00:00:00.000Z",
                    "properties": {
                        "Name": {"type": "title", "title": [{"plain_text": "Task A"}]},
                        "Status": {"type": "select", "select": {"name": "In progress"}},
                        "Date": {"type": "date", "date": {"start": "2026-08-03", "end": None}},
                        "Done": {"type": "checkbox", "checkbox": False},
                        "Tags": {"type": "multi_select", "multi_select": [{"name": "x"}, {"name": "y"}]},
                        "Note": {"type": "rich_text", "rich_text": [{"plain_text": "hello"}]},
                        "Empty": {"type": "select", "select": None},
                    },
                },
                {
                    "id": "row2", "url": "https://notion.so/row2",
                    "created_time": "2026-08-01T00:00:00.000Z",
                    "last_edited_time": "2026-08-02T00:00:00.000Z",
                    "properties": {
                        "Name": {"type": "title", "title": [{"plain_text": "Task B"}]},
                        "Status": {"type": "status", "status": {"name": "Done"}},
                        "Date": {"type": "date", "date": None},
                        "Done": {"type": "checkbox", "checkbox": True},
                        "Tags": {"type": "multi_select", "multi_select": []},
                        "Note": {"type": "rich_text", "rich_text": []},
                        "Empty": {"type": "select", "select": None},
                    },
                },
            ],
            "has_more": True,
            "next_cursor": "cursor-xyz",
        }


class QueryHandlerTest(unittest.TestCase):
    def setUp(self):
        self.handler = QueryHandler(client=FakeClient())

    def test_query_returns_parsed_rows(self):
        result = asyncio_run(self.handler.query_database("db1", page_size=50))
        self.assertNotIn("error", result)
        self.assertEqual(result["database_id"], "db1")
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], "cursor-xyz")
        rows = result["rows"]
        self.assertEqual(rows[0]["properties"]["Name"], "Task A")
        self.assertEqual(rows[0]["properties"]["Status"], "In progress")
        self.assertEqual(rows[0]["properties"]["Date"], {"start": "2026-08-03", "end": None})
        self.assertFalse(rows[0]["properties"]["Done"])
        self.assertEqual(rows[0]["properties"]["Tags"], ["x", "y"])
        self.assertEqual(rows[0]["properties"]["Note"], "hello")
        self.assertIsNone(rows[0]["properties"]["Empty"])
        # status type parsed too
        self.assertEqual(rows[1]["properties"]["Status"], "Done")
        self.assertTrue(rows[1]["properties"]["Done"])
        self.assertEqual(rows[1]["properties"]["Tags"], [])

    def test_query_handles_api_error(self):
        class ErrorClient:
            async def query_database(self, *a, **kw):
                raise RuntimeError("boom")
        handler = QueryHandler(client=ErrorClient())
        result = asyncio_run(handler.query_database("db1"))
        self.assertIn("error", result)
        self.assertEqual(result["code"], "QUERY_FAILED")

    def test_page_size_bounds(self):
        result = asyncio_run(self.handler.query_database("db1", page_size=0))
        self.assertEqual(result["total"], 2)  # clamps to 1, still returns both


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
