"""Offline tests for Notion user client methods, handler behavior, and MCP tools."""

import asyncio
import os
import sys
import unittest
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from core.notion_client import NOTION_VERSION, NotionClient
from handlers.user_handler import UserHandler
from handlers.validate import extract_user_id


USER_ID = "3aa0f42d7035802bbf61e7effd583a39"
DASHED_USER_ID = "3aa0f42d-7035-802b-bf61-e7effd583a39"
DASHED_BOT_ID = "38b0f42d-7035-808f-beb2-63f818eb3187"
PAGE_URL = f"https://www.notion.so/A-Page-{USER_ID}"


def run(coro):
    return asyncio.run(coro)


def person_user() -> dict:
    return {
        "object": "user",
        "id": DASHED_USER_ID,
        "name": "Ada Lovelace",
        "avatar_url": "https://cdn.example.test/ada.png",
        "type": "person",
        "person": {
            "email": "ada@example.test",
            "email_verified": True,
            "unexpected_private_value": "must-not-appear",
        },
        "unexpected_private_value": "must-not-appear",
    }


def bot_user() -> dict:
    return {
        "object": "user",
        "id": DASHED_BOT_ID,
        "name": "Example Integration",
        "avatar_url": None,
        "type": "bot",
        "bot": {
            "owner": {
                "type": "workspace",
                "workspace": True,
                "user": {"id": DASHED_USER_ID, "name": "Ada Lovelace"},
            },
            "workspace_name": "Example Workspace",
            "workspace_id": "a0b1c2d3-e4f5-6789-abcd-ef0123456789",
            "workspace_limits": {"max_file_upload_size_in_bytes": 5242880},
            "unexpected_private_value": "must-not-appear",
        },
    }


def formatted_person() -> dict:
    return {
        "id": DASHED_USER_ID,
        "name": "Ada Lovelace",
        "avatar_url": "https://cdn.example.test/ada.png",
        "type": "person",
        "email": "ada@example.test",
        "email_verified": True,
        "bot": None,
    }


def formatted_bot() -> dict:
    return {
        "id": DASHED_BOT_ID,
        "name": "Example Integration",
        "avatar_url": None,
        "type": "bot",
        "email": None,
        "email_verified": None,
        "bot": {
            "owner_type": "workspace",
            "workspace_name": "Example Workspace",
            "workspace_id": "a0b1c2d3-e4f5-6789-abcd-ef0123456789",
        },
    }


class TestUserClientEndpoints(unittest.TestCase):
    def test_user_methods_use_exact_get_paths_and_list_query_parameters(self):
        self.assertEqual(NOTION_VERSION, "2022-06-28")
        response = {"object": "user", "id": DASHED_USER_ID}

        async def exercise():
            client = NotionClient(token="test-token")
            calls = []

            async def fake_request(method, path, **kwargs):
                calls.append((method, path, kwargs))
                return response

            client._request = fake_request
            try:
                retrieved = await client.retrieve_user(USER_ID)
                listed_default = await client.list_users(page_size=250)
                listed_cursor = await client.list_users(
                    page_size=25, start_cursor="opaque-cursor")
                current = await client.get_self()
            finally:
                await client.close()
            return retrieved, listed_default, listed_cursor, current, calls

        retrieved, listed_default, listed_cursor, current, calls = run(exercise())

        self.assertIs(retrieved, response)
        self.assertIs(listed_default, response)
        self.assertIs(listed_cursor, response)
        self.assertIs(current, response)
        self.assertEqual(calls, [
            ("GET", f"users/{USER_ID}", {}),
            ("GET", "users", {"params": {"page_size": 100}}),
            ("GET", "users", {
                "params": {"page_size": 25, "start_cursor": "opaque-cursor"},
            }),
            ("GET", "users/me", {}),
        ])


class FakeUserClient:
    """Offline client that records every UserHandler request."""

    def __init__(self):
        self.self_calls = 0
        self.retrieve_calls = []
        self.list_calls = []
        self.self_response: Any = person_user()
        self.retrieve_response: Any = person_user()
        self.list_response: Any = {
            "results": [person_user(), bot_user()],
            "has_more": True,
            "next_cursor": "next-page",
        }
        self.self_exception: Optional[Exception] = None
        self.retrieve_exception: Optional[Exception] = None
        self.list_exception: Optional[Exception] = None

    async def get_self(self):
        self.self_calls += 1
        if self.self_exception is not None:
            raise self.self_exception
        return self.self_response

    async def retrieve_user(self, user_id: str):
        self.retrieve_calls.append(user_id)
        if self.retrieve_exception is not None:
            raise self.retrieve_exception
        return self.retrieve_response

    async def list_users(self, page_size: int = 100,
                         start_cursor: Optional[str] = None):
        self.list_calls.append((page_size, start_cursor))
        if self.list_exception is not None:
            raise self.list_exception
        return self.list_response


class TestUserHandler(unittest.TestCase):
    def setUp(self):
        self.client = FakeUserClient()
        self.handler = UserHandler(client=self.client)

    def test_get_self_and_get_user_return_allowlisted_stable_profiles(self):
        self.client.self_response = bot_user()
        self_result = run(self.handler.get_self())
        user_result = run(self.handler.get_user(DASHED_USER_ID))

        self.assertEqual(self.client.self_calls, 1)
        self.assertEqual(self.client.retrieve_calls, [USER_ID])
        self.assertEqual(self_result, formatted_bot())
        self.assertEqual(user_result, formatted_person())
        self.assertNotIn("unexpected_private_value", self_result)
        self.assertNotIn("unexpected_private_value", user_result)

    def test_get_users_clamps_page_size_and_preserves_pagination(self):
        result = run(self.handler.get_users(page_size=250, start_cursor="previous-page"))

        self.assertEqual(self.client.list_calls, [(100, "previous-page")])
        self.assertEqual(result, {
            "results": [formatted_person(), formatted_bot()],
            "has_more": True,
            "next_cursor": "next-page",
        })
        self.assertNotIn("unexpected_private_value", repr(result))

        run(self.handler.get_users(page_size=0))
        self.assertEqual(self.client.list_calls[-1], (1, None))

    def test_invalid_input_is_rejected_before_request_and_page_urls_are_not_users(self):
        bad_user = run(self.handler.get_user(PAGE_URL))
        bad_size = run(self.handler.get_users(page_size=True))
        bad_cursor = run(self.handler.get_users(start_cursor="   "))

        for result in (bad_user, bad_size, bad_cursor):
            self.assertTrue(result["error"])
            self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(self.client.retrieve_calls, [])
        self.assertEqual(self.client.list_calls, [])

    def test_api_errors_exceptions_and_malformed_responses_are_safe(self):
        api_error = {"error": True, "code": "FORBIDDEN", "message": "no user access"}
        self.client.retrieve_response = api_error
        self.assertIs(run(self.handler.get_user(USER_ID)), api_error)

        self.client.self_exception = RuntimeError("network down")
        self.assertEqual(run(self.handler.get_self()), {
            "error": True,
            "code": "FETCH_FAILED",
            "message": "network down",
        })

        self.client.list_response = {
            "results": [person_user()],
            "has_more": "yes",
            "next_cursor": "next-page",
        }
        malformed = run(self.handler.get_users())
        self.assertEqual(malformed["code"], "FETCH_FAILED")
        self.assertEqual(self.client.list_calls[-1], (100, None))


class TestUserServerInitialization(unittest.TestCase):
    def test_ensure_init_constructs_the_dedicated_user_handler(self):
        global_names = (
            "_client", "_cache", "_converter", "_batcher", "_comment_handler",
            "_file_handler", "_find_handler", "_query_handler", "_read_handler",
            "_user_handler", "_write_handler",
        )
        original_state = {name: getattr(server, name) for name in global_names}
        had_token = "NOTION_TOKEN" in os.environ
        original_token = os.environ.get("NOTION_TOKEN")
        try:
            for name in global_names:
                setattr(server, name, None)
            os.environ["NOTION_TOKEN"] = "offline-test-token"
            server._ensure_init()
            self.assertIsInstance(server._user_handler, UserHandler)
            assert server._user_handler is not None
            self.assertIs(server._user_handler.client, server._client)
        finally:
            created_client = server._client
            if created_client is not None:
                run(created_client.close())
            for name, value in original_state.items():
                setattr(server, name, value)
            if had_token:
                assert original_token is not None
                os.environ["NOTION_TOKEN"] = original_token
            else:
                os.environ.pop("NOTION_TOKEN", None)


class RecordingUserHandler:
    """Server-bound fake handler; it never makes Notion requests."""

    def __init__(self):
        self.self_calls = 0
        self.user_calls = []
        self.users_calls = []

    async def get_self(self):
        self.self_calls += 1
        return {"tool": "self"}

    async def get_user(self, user_id: str):
        self.user_calls.append(user_id)
        return {"tool": "user"}

    async def get_users(self, page_size: int = 100,
                        start_cursor: Optional[str] = None):
        self.users_calls.append((page_size, start_cursor))
        return {"tool": "users"}


class TestUserToolRegistrationAndValidation(unittest.TestCase):
    def setUp(self):
        self.handler = RecordingUserHandler()
        self.original_ensure_init = server._ensure_init
        self.original_user_handler = getattr(server, "_user_handler", None)
        server._ensure_init = lambda: None
        server._user_handler = self.handler

    def tearDown(self):
        server._ensure_init = self.original_ensure_init
        server._user_handler = self.original_user_handler

    def test_tools_are_registered(self):
        tools = {tool.name: tool for tool in run(server.mcp.list_tools())}
        self.assertTrue({
            "notion_get_self",
            "notion_get_user",
            "notion_get_users",
        }.issubset(tools))
        self.assertEqual(
            tools["notion_get_user"].inputSchema["required"], ["user_id"])
        self.assertEqual(
            set(tools["notion_get_users"].inputSchema["properties"]),
            {"page_size", "start_cursor"},
        )

    def test_user_ids_normalize_as_uuids_but_reject_page_urls(self):
        self.assertEqual(extract_user_id(DASHED_USER_ID), USER_ID)
        self.assertEqual(extract_user_id(USER_ID.upper()), USER_ID)
        self.assertIsNone(extract_user_id(PAGE_URL))

        self.assertEqual(run(server.notion_get_self()), {"tool": "self"})
        self.assertEqual(run(server.notion_get_user(DASHED_USER_ID)), {"tool": "user"})
        self.assertEqual(
            run(server.notion_get_users(page_size=25, start_cursor="opaque-cursor")),
            {"tool": "users"},
        )
        self.assertEqual(self.handler.self_calls, 1)
        self.assertEqual(self.handler.user_calls, [USER_ID])
        self.assertEqual(self.handler.users_calls, [(25, "opaque-cursor")])

        page_url_error = run(server.notion_get_user(PAGE_URL))
        self.assertTrue(page_url_error["error"])
        self.assertEqual(page_url_error["code"], "INVALID_INPUT")
        self.assertIn("page URLs", page_url_error["message"])
        self.assertEqual(self.handler.user_calls, [USER_ID])


if __name__ == "__main__":
    unittest.main()
