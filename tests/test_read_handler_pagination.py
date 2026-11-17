"""Offline cursor-pagination tests for Notion read tools."""

import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from mcp.server.fastmcp import FastMCP  # noqa: F401
except ModuleNotFoundError:
    # Keep this mocked tool-registration test usable with MCP SDK v2, where
    # the v1 FastMCP import used by the application is absent.
    class FastMCPTestDouble:
        def __init__(self, name):
            self.tools = []

        def tool(self):
            def register(function):
                self.tools.append(function)
                return function
            return register

        async def list_tools(self):
            return [types.SimpleNamespace(name=tool.__name__) for tool in self.tools]

    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = FastMCPTestDouble
    sys.modules["mcp.server.fastmcp"] = fastmcp_module

utils_package = types.ModuleType("utils")
utils_package.__path__ = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "utils")]
sys.modules["utils"] = utils_package

import server
from handlers.read_handler import ReadHandler

PAGE_ID = "3aa0f42d7035802bbf61e7effd583a39"


def run(coro):
    return asyncio.run(coro)


def block(block_id, has_children=False):
    return {
        "id": block_id,
        "type": "paragraph",
        "paragraph": {"rich_text": []},
        "has_children": has_children,
    }


class PagedClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def retrieve_page(self, page_id):
        return {"id": page_id, "properties": {}, "url": "https://example.test/page"}

    async def retrieve_block_children(self, block_id, page_size=100, start_cursor=None):
        self.calls.append((block_id, page_size, start_cursor))
        response = self.responses.get((block_id, start_cursor), self.responses.get(start_cursor))
        if callable(response):
            return response(page_size)
        return response


class ReadHandlerPaginationTests(unittest.TestCase):
    def test_read_page_continues_across_pages_without_gaps_or_duplicates(self):
        client = PagedClient({
            None: {"results": [block("b1"), block("b2")], "has_more": True, "next_cursor": "c2"},
            "c2": {"results": [block("b3")], "has_more": True, "next_cursor": "c3"},
            "c3": {"results": [block("b4")], "has_more": False, "next_cursor": None},
        })
        handler = ReadHandler(client)

        first = run(handler.read_page(PAGE_ID, max_blocks=3, depth=1))
        second = run(handler.read_page(PAGE_ID, max_blocks=3, depth=1,
                                       start_cursor=first["next_cursor"]))

        self.assertEqual([item["id"] for item in first["blocks"]], ["b1", "b2", "b3"])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_cursor"], "c3")
        self.assertEqual([item["id"] for item in second["blocks"]], ["b4"])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(client.calls, [
            (PAGE_ID, 3, None), (PAGE_ID, 1, "c2"), (PAGE_ID, 3, "c3"),
        ])

    def test_pages_fetched_counts_root_pages_and_nested_pages(self):
        client = PagedClient({
            (PAGE_ID, None): {"results": [block("parent", has_children=True)],
                             "has_more": True, "next_cursor": "root-next"},
            (PAGE_ID, "root-next"): {"results": [block("root-child")],
                                         "has_more": False, "next_cursor": None},
            ("parent", None): {"results": [block("nested-1")],
                                  "has_more": True, "next_cursor": "nested-next"},
            ("parent", "nested-next"): {"results": [block("nested-2")],
                                                "has_more": False, "next_cursor": None},
        })
        result = run(ReadHandler(client).read_page(PAGE_ID, max_blocks=10, depth=2))

        self.assertEqual(result["pages_fetched"], 4)
        self.assertEqual([item["id"] for item in result["blocks"]], ["parent", "root-child"])
        self.assertEqual([item["id"] for item in result["blocks"][0]["children"]],
                         ["nested-1", "nested-2"])
        self.assertEqual(client.calls, [
            (PAGE_ID, 10, None), ("parent", 10, None),
            ("parent", 9, "nested-next"), (PAGE_ID, 9, "root-next"),
        ])

    def test_read_page_at_exact_cap_preserves_verified_continuation(self):
        client = PagedClient({
            None: {"results": [block("b1"), block("b2")], "has_more": True, "next_cursor": "next"},
        })
        result = run(ReadHandler(client).read_page(PAGE_ID, max_blocks=2, depth=1))

        self.assertEqual([item["id"] for item in result["blocks"]], ["b1", "b2"])
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], "next")
        self.assertEqual(result["pages_fetched"], 1)
        self.assertEqual(client.calls, [(PAGE_ID, 2, None)])

    def test_read_page_end_of_page_returns_no_cursor(self):
        client = PagedClient({
            None: {"results": [block("b1")], "has_more": False, "next_cursor": None},
        })
        result = run(ReadHandler(client).read_page(PAGE_ID, max_blocks=10, depth=1))

        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])

    def test_missing_cursor_while_more_data_exists_fails_closed(self):
        client = PagedClient({
            None: {"results": [block("b1")], "has_more": True, "next_cursor": None},
        })
        result = run(ReadHandler(client).read_page(PAGE_ID, max_blocks=10, depth=1))

        self.assertTrue(result["error"])
        self.assertEqual(result["code"], "READBACK_INCOMPLETE")
        self.assertNotIn("has_more", result)

    def test_supplied_root_cursor_is_forwarded(self):
        client = PagedClient({
            "resume-token": {"results": [block("b2")], "has_more": False},
        })
        result = run(ReadHandler(client).read_page(
            PAGE_ID, max_blocks=10, depth=1, start_cursor="resume-token"))
        self.assertEqual(client.calls, [(PAGE_ID, 10, "resume-token")])
        self.assertEqual(result["blocks"][0]["id"], "b2")


class ChildBlocksToolTests(unittest.TestCase):
    def test_handler_forwards_bounded_page_arguments_and_cursors(self):
        client = PagedClient({
            "start": {"results": [block("child")], "has_more": True, "next_cursor": "next"},
        })
        result = run(ReadHandler(client).get_block_children(
            PAGE_ID, page_size=25, start_cursor="start"))

        self.assertEqual(client.calls, [(PAGE_ID, 25, "start")])
        self.assertEqual(result["results"][0]["id"], "child")
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], "next")

    def test_one_page_response_reports_pages_fetched_one(self):
        client = PagedClient({
            None: {"results": [block("child")], "has_more": False, "next_cursor": None},
        })
        result = run(ReadHandler(client).get_block_children(PAGE_ID, page_size=20))

        self.assertEqual(result["pages_fetched"], 1)
        self.assertFalse(result["has_more"])
        self.assertEqual(client.calls, [(PAGE_ID, 20, None)])

    def test_malformed_has_more_without_cursor_fails_closed(self):
        client = PagedClient({
            None: {"results": [block("child")], "has_more": True},
        })
        result = run(ReadHandler(client).get_block_children(PAGE_ID))
        self.assertTrue(result["error"])
        self.assertEqual(result["code"], "READBACK_INCOMPLETE")

    def test_tool_is_registered_and_passes_resolved_cursor_arguments(self):
        class RecordingHandler:
            def __init__(self):
                self.calls = []

            async def get_block_children(self, block_id, page_size, start_cursor):
                self.calls.append((block_id, page_size, start_cursor))
                return {"block_id": block_id, "results": [], "has_more": False, "next_cursor": None}

        handler = RecordingHandler()
        old_init, old_handler = server._ensure_init, server._read_handler
        server._ensure_init, server._read_handler = lambda: None, handler
        try:
            names = {tool.name for tool in run(server.mcp.list_tools())}
            self.assertIn("notion_get_block_children", names)
            result = run(server.notion_get_block_children(
                PAGE_ID, page_size=17, start_cursor="cursor-token"))
        finally:
            server._ensure_init, server._read_handler = old_init, old_handler

        self.assertFalse(result.get("error", False))
        self.assertEqual(handler.calls, [(PAGE_ID, 17, "cursor-token")])


if __name__ == "__main__":
    unittest.main()
