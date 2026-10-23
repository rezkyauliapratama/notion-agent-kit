"""Offline tests for Notion media-block download URL handling."""

import asyncio
import os
import sys
import unittest
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from handlers.file_handler import FileHandler, SUPPORTED_MEDIA_BLOCK_TYPES


BLOCK_ID = "fedcba9876543210fedcba9876543210"
DASHED_BLOCK_ID = "fedcba98-7654-3210-fedc-ba9876543210"
EXPIRY_TIME = "2026-10-24T00:00:00.000Z"
HOSTED_URL = (
    "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/example.png"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc123"
)
EXTERNAL_URL = "https://cdn.example.test/assets/example.png?version=1"
EXPECTED_MEDIA_BLOCK_TYPES = ("image", "file", "video", "pdf", "audio")


def run(coro):
    return asyncio.run(coro)


def media_block(block_type: str, source_type: str, url: Any = HOSTED_URL,
                expiry_time: Any = None) -> dict:
    """Build one Retrieve block response in the Notion media-block shape."""
    file_object = {"url": url}
    if expiry_time is not None:
        file_object["expiry_time"] = expiry_time
    return {
        "object": "block",
        "id": BLOCK_ID,
        "type": block_type,
        block_type: {
            "type": source_type,
            source_type: file_object,
        },
    }


class FakeFileClient:
    """Fake client exposing only the existing retrieve_block client method."""

    def __init__(self, response: Optional[dict[str, Any]] = None):
        self.response = response
        self.calls = []
        self.exception: Optional[Exception] = None

    async def retrieve_block(self, block_id: str):
        self.calls.append(block_id)
        if self.exception is not None:
            raise self.exception
        return self.response


class TestFileHandler(unittest.TestCase):
    def test_all_supported_block_types_return_hosted_signed_url_and_expiry(self):
        self.assertEqual(SUPPORTED_MEDIA_BLOCK_TYPES, frozenset(EXPECTED_MEDIA_BLOCK_TYPES))
        for block_type in EXPECTED_MEDIA_BLOCK_TYPES:
            with self.subTest(block_type=block_type):
                client = FakeFileClient(media_block(
                    block_type, "file", HOSTED_URL, EXPIRY_TIME))
                result = run(FileHandler(client).get_block_download_urls(BLOCK_ID))

                self.assertEqual(client.calls, [BLOCK_ID])
                self.assertEqual(result, {
                    "block_id": BLOCK_ID,
                    "type": block_type,
                    "urls": [{
                        "url": HOSTED_URL,
                        "source": "file",
                        "expiry_time": EXPIRY_TIME,
                    }],
                })

    def test_all_supported_block_types_return_external_url(self):
        for block_type in EXPECTED_MEDIA_BLOCK_TYPES:
            with self.subTest(block_type=block_type):
                client = FakeFileClient(media_block(block_type, "external", EXTERNAL_URL))
                result = run(FileHandler(client).get_block_download_urls(BLOCK_ID))

                self.assertEqual(client.calls, [BLOCK_ID])
                self.assertEqual(result, {
                    "block_id": BLOCK_ID,
                    "type": block_type,
                    "urls": [{
                        "url": EXTERNAL_URL,
                        "source": "external",
                    }],
                })

    def test_uses_the_file_object_selected_by_notion_type(self):
        block = media_block("image", "external", EXTERNAL_URL)
        block["image"]["file"] = {"url": HOSTED_URL, "expiry_time": EXPIRY_TIME}

        result = run(FileHandler(FakeFileClient(block)).get_block_download_urls(BLOCK_ID))

        self.assertEqual(result["urls"], [{
            "url": EXTERNAL_URL,
            "source": "external",
        }])

    def test_hosted_url_without_expiry_omits_optional_expiry_metadata(self):
        client = FakeFileClient(media_block("image", "file", HOSTED_URL))

        result = run(FileHandler(client).get_block_download_urls(BLOCK_ID))

        self.assertEqual(result["urls"], [{"url": HOSTED_URL, "source": "file"}])

    def test_rejects_empty_block_id_before_calling_client(self):
        client = FakeFileClient(media_block("image", "file", HOSTED_URL))

        result = run(FileHandler(client).get_block_download_urls("  "))

        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(client.calls, [])

    def test_rejects_unsupported_block_type(self):
        client = FakeFileClient({
            "object": "block",
            "id": BLOCK_ID,
            "type": "paragraph",
            "paragraph": {"rich_text": []},
        })

        result = run(FileHandler(client).get_block_download_urls(BLOCK_ID))

        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertIn("paragraph", result["message"])
        self.assertIn("image", result["message"])

    def test_rejects_missing_or_malformed_urls(self):
        hosted_without_url = media_block("image", "file")
        external_without_url = media_block("image", "external")
        del hosted_without_url["image"]["file"]["url"]
        del external_without_url["image"]["external"]["url"]
        cases = (
            ("hosted_missing", hosted_without_url),
            ("external_missing", external_without_url),
            ("null", media_block("image", "file", None)),
            ("blank", media_block("image", "file", "")),
            ("not_absolute", media_block("image", "file", "example.test/image.png")),
            ("unsupported_scheme", media_block(
                "image", "external", "ftp://example.test/image.png")),
            ("invalid_port", media_block(
                "image", "external", "https://example.test:not-a-port/image.png")),
            ("contains_whitespace", media_block(
                "image", "file", "https://example.test/image file.png")),
        )
        for name, block in cases:
            with self.subTest(name=name):
                result = run(FileHandler(FakeFileClient(block)).get_block_download_urls(BLOCK_ID))

                self.assertEqual(result["code"], "INVALID_INPUT")
                self.assertIn("URL", result["message"])

    def test_rejects_malformed_block_data_and_unknown_source_type(self):
        malformed_file_object = {
            "id": BLOCK_ID,
            "type": "audio",
            "audio": {"type": "file", "file": "not-an-object"},
        }
        unknown_source_type = {
            "id": BLOCK_ID,
            "type": "pdf",
            "pdf": {"type": "file_upload", "file_upload": {"id": "upload-1"}},
        }
        malformed_block_type = {"id": BLOCK_ID, "type": {"not": "a string"}}
        malformed_source_type = {
            "id": BLOCK_ID,
            "type": "video",
            "video": {"type": ["file"], "file": {"url": HOSTED_URL}},
        }
        cases = (
            ("file_object", malformed_file_object),
            ("source_type", unknown_source_type),
            ("block_type", malformed_block_type),
            ("source_type_shape", malformed_source_type),
        )
        for name, block in cases:
            with self.subTest(name=name):
                result = run(FileHandler(FakeFileClient(block)).get_block_download_urls(BLOCK_ID))
                self.assertEqual(result["code"], "INVALID_INPUT")

    def test_propagates_api_error_and_maps_client_exception(self):
        api_error = {"error": True, "code": "FORBIDDEN", "message": "not shared"}
        error_client = FakeFileClient(api_error)
        self.assertIs(
            run(FileHandler(error_client).get_block_download_urls(BLOCK_ID)), api_error)

        exception_client = FakeFileClient()
        exception_client.exception = RuntimeError("network down")
        result = run(FileHandler(exception_client).get_block_download_urls(BLOCK_ID))
        self.assertEqual(result, {
            "error": True,
            "code": "FETCH_FAILED",
            "message": "network down",
        })


class RecordingFileHandler:
    """Server-bound fake handler; no Notion requests are made."""

    def __init__(self):
        self.calls = []

    async def get_block_download_urls(self, block_id: str):
        self.calls.append(block_id)
        return {"ok": True}


class TestFileToolRegistrationAndValidation(unittest.TestCase):
    def setUp(self):
        self.handler = RecordingFileHandler()
        self.original_ensure_init = server._ensure_init
        self.original_file_handler = getattr(server, "_file_handler", None)
        server._ensure_init = lambda: None
        server._file_handler = self.handler

    def tearDown(self):
        server._ensure_init = self.original_ensure_init
        server._file_handler = self.original_file_handler

    def test_tool_is_registered(self):
        names = {tool.name for tool in run(server.mcp.list_tools())}
        self.assertIn("notion_get_block_download_urls", names)

    def test_tool_normalizes_block_ids_and_rejects_invalid_ids_before_handler(self):
        block_url = f"https://www.notion.so/Media-{BLOCK_ID}"
        self.assertEqual(run(server.notion_get_block_download_urls(block_url)), {"ok": True})
        self.assertEqual(run(server.notion_get_block_download_urls(DASHED_BLOCK_ID)), {"ok": True})
        self.assertEqual(self.handler.calls, [BLOCK_ID, BLOCK_ID])

        result = run(server.notion_get_block_download_urls("not-an-id"))
        self.assertTrue(result["error"])
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(self.handler.calls, [BLOCK_ID, BLOCK_ID])


if __name__ == "__main__":
    unittest.main()
