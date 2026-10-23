"""Offline tests for Notion comment tools and handler behavior."""

import asyncio
import os
import sys
import unittest
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from handlers.comment_handler import CommentHandler, MAX_RICH_TEXT_ITEMS, RICH_TEXT_MAX


PAGE_ID = "0123456789abcdef0123456789abcdef"
DASHED_PAGE_ID = "01234567-89ab-cdef-0123-456789abcdef"
BLOCK_ID = "fedcba9876543210fedcba9876543210"
DISCUSSION_ID = "abcdef0123456789abcdef0123456789"


def run(coro):
    return asyncio.run(coro)


class FakeCommentClient:
    """Offline comment client that records exact handler arguments."""

    def __init__(self):
        self.create_calls = []
        self.retrieve_calls = []
        self.create_response: Optional[dict[str, Any]] = None
        self.retrieve_response: Optional[dict[str, Any]] = None
        self.create_exception: Optional[Exception] = None
        self.retrieve_exception: Optional[Exception] = None

    async def create_comment(self, rich_text, parent=None, discussion_id=None):
        self.create_calls.append({
            "rich_text": rich_text,
            "parent": parent,
            "discussion_id": discussion_id,
        })
        if self.create_exception:
            raise self.create_exception
        if self.create_response is not None:
            return self.create_response
        return {
            "object": "comment",
            "id": "comment-1",
            "discussion_id": discussion_id or "discussion-1",
            "parent": parent,
            "rich_text": rich_text,
            "created_time": "2026-10-23T00:00:00.000Z",
            "last_edited_time": "2026-10-23T00:00:00.000Z",
            "created_by": {"object": "user", "id": "user-1"},
        }

    async def retrieve_comments(self, block_id, page_size=100):
        self.retrieve_calls.append((block_id, page_size))
        if self.retrieve_exception:
            raise self.retrieve_exception
        if self.retrieve_response is not None:
            return self.retrieve_response
        return {"results": [], "has_more": False, "next_cursor": None}


class TestCommentHandlerCreate(unittest.TestCase):
    def setUp(self):
        self.client = FakeCommentClient()
        self.handler = CommentHandler(client=self.client)

    def test_page_comment_builds_rich_text_and_parent(self):
        result = run(self.handler.create_comment("Hello from a page", page_id=PAGE_ID))

        self.assertEqual(self.client.create_calls, [{
            "rich_text": [{"type": "text", "text": {"content": "Hello from a page"}}],
            "parent": {"type": "page_id", "page_id": PAGE_ID},
            "discussion_id": None,
        }])
        self.assertEqual(result["comment_id"], "comment-1")
        self.assertEqual(result["discussion_id"], "discussion-1")
        self.assertEqual(result["parent"], {"type": "page_id", "page_id": PAGE_ID})
        self.assertEqual(result["text"], "Hello from a page")
        self.assertEqual(result["created"], "2026-10-23T00:00:00.000Z")

    def test_block_comment_and_discussion_reply_use_exactly_one_target(self):
        block_result = run(self.handler.create_comment("On a block", block_id=BLOCK_ID))
        reply_result = run(self.handler.create_comment("A reply", discussion_id=DISCUSSION_ID))

        self.assertEqual(block_result["parent"], {"type": "block_id", "block_id": BLOCK_ID})
        self.assertEqual(self.client.create_calls[0]["parent"],
                         {"type": "block_id", "block_id": BLOCK_ID})
        self.assertIsNone(self.client.create_calls[0]["discussion_id"])
        self.assertEqual(reply_result["discussion_id"], DISCUSSION_ID)
        self.assertIsNone(self.client.create_calls[1]["parent"])
        self.assertEqual(self.client.create_calls[1]["discussion_id"], DISCUSSION_ID)

    def test_create_validates_content_and_target_combination_before_calling_client(self):
        cases = [
            ("", {"page_id": PAGE_ID}),
            ("   ", {"page_id": PAGE_ID}),
            ("text", {}),
            ("text", {"page_id": PAGE_ID, "block_id": BLOCK_ID}),
            ("text", {"discussion_id": ""}),
        ]
        for text, kwargs in cases:
            with self.subTest(text=text, kwargs=kwargs):
                result = run(self.handler.create_comment(text, **kwargs))
                self.assertTrue(result["error"])
                self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(self.client.create_calls, [])

    def test_create_splits_long_text_and_rejects_over_limit(self):
        text = "x" * (RICH_TEXT_MAX + 1)
        result = run(self.handler.create_comment(text, page_id=PAGE_ID))
        rich_text = self.client.create_calls[0]["rich_text"]
        self.assertEqual([len(part["text"]["content"]) for part in rich_text],
                         [RICH_TEXT_MAX, 1])
        self.assertEqual(result["text"], text)

        too_long = "x" * (RICH_TEXT_MAX * MAX_RICH_TEXT_ITEMS + 1)
        result = run(self.handler.create_comment(too_long, page_id=PAGE_ID))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(len(self.client.create_calls), 1)

    def test_create_propagates_client_error_and_maps_exception(self):
        api_error = {"error": True, "code": "FORBIDDEN", "message": "comments disabled"}
        self.client.create_response = api_error
        self.assertIs(run(self.handler.create_comment("text", page_id=PAGE_ID)), api_error)

        self.client.create_response = None
        self.client.create_exception = RuntimeError("network down")
        result = run(self.handler.create_comment("text", page_id=PAGE_ID))
        self.assertEqual(result, {"error": True, "code": "CREATE_FAILED", "message": "network down"})


class TestCommentHandlerGet(unittest.TestCase):
    def setUp(self):
        self.client = FakeCommentClient()
        self.handler = CommentHandler(client=self.client)

    def test_get_formats_comments_and_preserves_pagination(self):
        self.client.retrieve_response = {
            "results": [
                {
                    "id": "comment-1",
                    "discussion_id": "thread-1",
                    "parent": {"type": "page_id", "page_id": PAGE_ID},
                    "created_time": "2026-10-22T00:00:00.000Z",
                    "last_edited_time": "2026-10-22T01:00:00.000Z",
                    "created_by": {"object": "user", "id": "user-1"},
                    "rich_text": [{"plain_text": "First comment"}],
                },
                {
                    "id": "comment-2",
                    "discussion_id": "thread-1",
                    "rich_text": [{"type": "text", "text": {"content": "Second comment"}}],
                },
            ],
            "has_more": True,
            "next_cursor": "cursor-2",
        }

        result = run(self.handler.get_comments(BLOCK_ID, page_size=500))

        self.assertEqual(self.client.retrieve_calls, [(BLOCK_ID, 100)])
        self.assertEqual(result["block_id"], BLOCK_ID)
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], "cursor-2")
        self.assertEqual(result["comments"][0]["text"], "First comment")
        self.assertEqual(result["comments"][1]["text"], "Second comment")
        self.assertEqual(result["comments"][0]["created_by"],
                         {"object": "user", "id": "user-1"})

    def test_get_clamps_low_page_size_and_validates_arguments(self):
        run(self.handler.get_comments(BLOCK_ID, page_size=0))
        self.assertEqual(self.client.retrieve_calls, [(BLOCK_ID, 1)])

        invalid_cases = (("", 10), (BLOCK_ID, "10"), (BLOCK_ID, True))
        for block_id, page_size in invalid_cases:
            with self.subTest(block_id=block_id, page_size=page_size):
                result = run(self.handler.get_comments(block_id, page_size=page_size))  # type: ignore[arg-type]
                self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(len(self.client.retrieve_calls), 1)

    def test_get_propagates_client_error_and_maps_exception(self):
        api_error = {"error": True, "code": "FORBIDDEN", "message": "comments disabled"}
        self.client.retrieve_response = api_error
        self.assertIs(run(self.handler.get_comments(BLOCK_ID)), api_error)

        self.client.retrieve_response = None
        self.client.retrieve_exception = RuntimeError("network down")
        result = run(self.handler.get_comments(BLOCK_ID))
        self.assertEqual(result, {"error": True, "code": "FETCH_FAILED", "message": "network down"})


class RecordingToolHandler:
    """Server-bound fake handler; no Notion requests are made."""

    def __init__(self):
        self.create_calls = []
        self.get_calls = []

    async def create_comment(self, text, page_id=None, block_id=None, discussion_id=None):
        self.create_calls.append((text, page_id, block_id, discussion_id))
        return {"ok": True}

    async def get_comments(self, block_id, page_size=100):
        self.get_calls.append((block_id, page_size))
        return {"ok": True}


class TestCommentToolRegistrationAndValidation(unittest.TestCase):
    def setUp(self):
        self.handler = RecordingToolHandler()
        self.original_ensure_init = server._ensure_init
        self.original_comment_handler = server._comment_handler
        server._ensure_init = lambda: None
        server._comment_handler = self.handler

    def tearDown(self):
        server._ensure_init = self.original_ensure_init
        server._comment_handler = self.original_comment_handler

    def test_tools_are_registered(self):
        names = {tool.name for tool in run(server.mcp.list_tools())}
        self.assertIn("notion_create_comment", names)
        self.assertIn("notion_get_comments", names)

    def test_create_tool_resolves_page_block_and_discussion_ids(self):
        page_url = f"https://www.notion.so/Title-{PAGE_ID}"
        run(server.notion_create_comment("page", page_id=page_url))
        run(server.notion_create_comment("block", block_id=DASHED_PAGE_ID))
        run(server.notion_create_comment("reply", discussion_id=DASHED_PAGE_ID))

        self.assertEqual(self.handler.create_calls, [
            ("page", PAGE_ID, None, None),
            ("block", None, PAGE_ID, None),
            ("reply", None, None, PAGE_ID),
        ])

    def test_get_tool_resolves_id_and_rejects_invalid_id_before_handler(self):
        run(server.notion_get_comments(DASHED_PAGE_ID, page_size=25))
        self.assertEqual(self.handler.get_calls, [(PAGE_ID, 25)])

        result = run(server.notion_get_comments("not-an-id"))
        self.assertTrue(result["error"])
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(self.handler.get_calls, [(PAGE_ID, 25)])


if __name__ == "__main__":
    unittest.main()
