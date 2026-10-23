"""Offline tests for the Notion page-move client, handler, and MCP tool."""

import asyncio
import os
import sys
import unittest
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from core.notion_client import NOTION_VERSION, NotionClient
from handlers.write_handler import WriteHandler


PAGE_ID = "0123456789abcdef0123456789abcdef"
PARENT_PAGE_ID = "fedcba9876543210fedcba9876543210"
DASHED_PARENT_PAGE_ID = "fedcba98-7654-3210-fedc-ba9876543210"


def run(coro):
    return asyncio.run(coro)


class TestMovePageClient(unittest.TestCase):
    def test_move_page_uses_post_move_endpoint_and_page_parent_body(self):
        self.assertEqual(NOTION_VERSION, "2022-06-28")
        response = {
            "object": "page",
            "id": PAGE_ID,
            "parent": {"type": "page_id", "page_id": PARENT_PAGE_ID},
        }

        async def exercise():
            client = NotionClient(token="test-token")
            calls = []

            async def fake_request(method, path, **kwargs):
                calls.append((method, path, kwargs))
                return response

            client._request = fake_request
            try:
                result = await client.move_page(PAGE_ID, PARENT_PAGE_ID)
            finally:
                await client.close()
            return result, calls

        result, calls = run(exercise())

        self.assertIs(result, response)
        self.assertEqual(calls, [
            ("POST", f"pages/{PAGE_ID}/move", {
                "json": {"parent": {"page_id": PARENT_PAGE_ID}},
            }),
        ])
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith(f"/{PAGE_ID}/move"))


class FakeMoveClient:
    """Offline client recording handler calls without making API requests."""

    def __init__(self, response: Optional[dict[str, Any]] = None):
        self.response = response
        self.calls = []
        self.exception: Optional[Exception] = None

    async def move_page(self, page_id: str, parent_page_id: str):
        self.calls.append((page_id, parent_page_id))
        if self.exception is not None:
            raise self.exception
        return self.response


class TestMovePageHandler(unittest.TestCase):
    def test_returns_stable_page_parent_and_raw_response(self):
        response = {
            "object": "page",
            "id": PAGE_ID,
            "parent": {"type": "page_id", "page_id": PARENT_PAGE_ID},
            "url": "https://www.notion.so/example",
        }
        client = FakeMoveClient(response)
        handler = WriteHandler(client=client, converter=None, batcher=None)

        result = run(handler.move_page(PAGE_ID, PARENT_PAGE_ID))

        self.assertEqual(client.calls, [(PAGE_ID, PARENT_PAGE_ID)])
        self.assertEqual(result, {
            "page_id": PAGE_ID,
            "parent": {"type": "page_id", "page_id": PARENT_PAGE_ID},
            "response": response,
        })

    def test_propagates_api_errors_and_maps_client_exceptions(self):
        api_error = {
            "error": True,
            "code": "FORBIDDEN",
            "message": "Integration does not have access.",
        }
        client = FakeMoveClient(api_error)
        handler = WriteHandler(client=client, converter=None, batcher=None)

        self.assertIs(run(handler.move_page(PAGE_ID, PARENT_PAGE_ID)), api_error)

        client.response = None
        client.exception = RuntimeError("network down")
        result = run(handler.move_page(PAGE_ID, PARENT_PAGE_ID))
        self.assertEqual(result, {
            "error": True,
            "code": "UPDATE_FAILED",
            "message": "network down",
        })

    def test_rejects_empty_ids_before_calling_client(self):
        client = FakeMoveClient({})
        handler = WriteHandler(client=client, converter=None, batcher=None)

        page_error = run(handler.move_page(" ", PARENT_PAGE_ID))
        parent_error = run(handler.move_page(PAGE_ID, ""))

        self.assertEqual(page_error, {
            "error": True,
            "code": "INVALID_INPUT",
            "message": "page_id is required",
        })
        self.assertEqual(parent_error, {
            "error": True,
            "code": "INVALID_INPUT",
            "message": "parent_page_id is required",
        })
        self.assertEqual(client.calls, [])


class RecordingMoveHandler:
    """Server-bound fake write handler; no Notion requests are made."""

    def __init__(self):
        self.calls = []

    async def move_page(self, page_id: str, parent_page_id: str):
        self.calls.append((page_id, parent_page_id))
        return {"ok": True}


class TestMovePageToolRegistrationAndValidation(unittest.TestCase):
    def setUp(self):
        self.handler = RecordingMoveHandler()
        self.original_ensure_init = server._ensure_init
        self.original_write_handler = server._write_handler
        server._ensure_init = lambda: None
        server._write_handler = self.handler

    def tearDown(self):
        server._ensure_init = self.original_ensure_init
        server._write_handler = self.original_write_handler

    def test_tool_is_registered(self):
        names = {tool.name for tool in run(server.mcp.list_tools())}
        self.assertIn("notion_move_page", names)

    def test_tool_normalizes_both_ids_and_rejects_invalid_ids_before_handler(self):
        page_url = f"https://www.notion.so/Moved-{PAGE_ID}"
        self.assertEqual(
            run(server.notion_move_page(page_url, DASHED_PARENT_PAGE_ID)),
            {"ok": True},
        )
        self.assertEqual(self.handler.calls, [(PAGE_ID, PARENT_PAGE_ID)])

        page_error = run(server.notion_move_page("not-an-id", PARENT_PAGE_ID))
        parent_error = run(server.notion_move_page(PAGE_ID, "not-an-id"))
        self.assertTrue(page_error["error"])
        self.assertEqual(page_error["code"], "INVALID_INPUT")
        self.assertTrue(parent_error["error"])
        self.assertEqual(parent_error["code"], "INVALID_INPUT")
        self.assertEqual(self.handler.calls, [(PAGE_ID, PARENT_PAGE_ID)])


if __name__ == "__main__":
    unittest.main()
