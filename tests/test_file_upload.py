"""Offline tests for the single-part local Notion file upload flow."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import server
from core.notion_client import NOTION_VERSION, NotionClient
from handlers.file_handler import (
    MAX_FILENAME_BYTES,
    MAX_SINGLE_PART_BYTES,
    FileHandler,
)


PAGE_ID = "0123456789abcdef0123456789abcdef"
DASHED_PAGE_ID = "01234567-89ab-cdef-0123-456789abcdef"
UPLOAD_ID = "abcdef0123456789abcdef0123456789"


def run(coro):
    return asyncio.run(coro)


class TestFileUploadClient(unittest.TestCase):
    def test_create_uses_single_part_json_endpoint_and_version(self):
        client = NotionClient("secret-token")
        calls = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"id": UPLOAD_ID, "status": "pending"}

        client._request = fake_request
        try:
            result = run(client.create_file_upload("report.pdf", "application/pdf"))
        finally:
            run(client.close())

        self.assertEqual(result["id"], UPLOAD_ID)
        self.assertEqual(calls, [("POST", "file_uploads", {
            "json": {
                "mode": "single_part",
                "filename": "report.pdf",
                "content_type": "application/pdf",
            },
        })])
        self.assertEqual(NOTION_VERSION, "2022-06-28")

    def test_send_uses_exact_path_and_generated_multipart_payload(self):
        observed = []

        async def transport(request):
            observed.append({
                "method": request.method,
                "path": request.url.path,
                "version": request.headers.get("Notion-Version"),
                "authorization": request.headers.get("Authorization"),
                "content_type": request.headers.get("Content-Type"),
                "body": await request.aread(),
            })
            return httpx.Response(200, json={"id": UPLOAD_ID, "status": "uploaded"})

        client = NotionClient("secret-token")
        old_client = client._client
        run(old_client.aclose())
        client._client = httpx.AsyncClient(
            base_url="https://api.notion.com/v1/",
            headers={"Authorization": "Bearer secret-token", "Notion-Version": NOTION_VERSION},
            transport=httpx.MockTransport(transport),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            path.write_bytes(b"hello upload")
            try:
                result = run(client.send_file_upload(
                    UPLOAD_ID, path, path.name, "text/plain"))
            finally:
                run(client.close())

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(len(observed), 1)
        request = observed[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], f"/v1/file_uploads/{UPLOAD_ID}/send")
        self.assertEqual(request["version"], NOTION_VERSION)
        self.assertEqual(request["authorization"], "Bearer secret-token")
        self.assertTrue(request["content_type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'filename="report.txt"', request["body"])
        self.assertIn(b"hello upload", request["body"])
        self.assertNotIn(b"part_number", request["body"])


class UploadClient:
    def __init__(self):
        self.calls = []
        self.create_response = {"id": UPLOAD_ID, "status": "pending"}
        self.send_response = {"id": UPLOAD_ID, "status": "uploaded"}
        self.attach_response = {"results": [{"id": "block-uploaded"}]}
        self.exception: Optional[Exception] = None

    async def create_file_upload(self, filename, content_type):
        self.calls.append(("create", filename, content_type))
        if self.exception:
            raise self.exception
        return self.create_response

    async def send_file_upload(self, upload_id, file_path, filename, content_type):
        self.calls.append(("send", upload_id, file_path, filename, content_type))
        if self.exception:
            raise self.exception
        return self.send_response

    async def append_block_children(self, target_id, children):
        self.calls.append(("attach", target_id, children))
        if self.exception:
            raise self.exception
        return self.attach_response


class TestFileUploadHandler(unittest.TestCase):
    def setUp(self):
        self.client = UploadClient()
        self.handler = FileHandler(self.client)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "report.pdf"
        self.path.write_bytes(b"pdf bytes")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_upload_creates_sends_and_attaches_exact_file_upload_block(self):
        result = run(self.handler.upload_file(
            str(self.path), f"https://www.notion.so/Report-{PAGE_ID}"))

        self.assertEqual(result, {
            "upload_id": UPLOAD_ID,
            "block_id": "block-uploaded",
            "target_id": PAGE_ID,
            "target_type": "page",
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "content_length": len(b"pdf bytes"),
            "block_type": "pdf",
        })
        self.assertEqual(self.client.calls[0],
                         ("create", "report.pdf", "application/pdf"))
        self.assertEqual(self.client.calls[1][0:2], ("send", UPLOAD_ID))
        self.assertEqual(self.client.calls[1][3:],
                         ("report.pdf", "application/pdf"))
        self.assertEqual(self.client.calls[2], (
            "attach", PAGE_ID, [{
                "object": "block",
                "type": "pdf",
                "pdf": {
                    "type": "file_upload",
                    "file_upload": {"id": UPLOAD_ID},
                },
            }]))

    def test_explicit_mime_and_block_target_are_preserved(self):
        path = Path(self.temp_dir.name) / "asset.bin"
        path.write_bytes(b"binary")
        result = run(self.handler.upload_file(
            str(path), DASHED_PAGE_ID, target_type="BLOCK",
            mime_type="application/custom"))

        self.assertEqual(result["target_id"], PAGE_ID)
        self.assertEqual(result["target_type"], "block")
        self.assertEqual(result["content_type"], "application/custom")
        self.assertEqual(result["block_type"], "file")
        self.assertEqual(self.client.calls[2][2][0]["file"], {
            "type": "file_upload", "file_upload": {"id": UPLOAD_ID}})

    def test_validation_rejects_bad_path_target_size_name_and_mime_before_api(self):
        cases = [
            ("", PAGE_ID, "page", None),
            (str(Path(self.temp_dir.name) / "missing.bin"), PAGE_ID, "page", None),
            (str(self.temp_dir.name), PAGE_ID, "page", None),
            (str(self.path), "not-an-id", "page", None),
            (str(self.path), PAGE_ID, "database", None),
            (str(self.path), PAGE_ID, "page", "not a mime"),
        ]
        for file_path, target_id, target_type, mime_type in cases:
            with self.subTest(file_path=file_path, target_id=target_id):
                result = run(self.handler.upload_file(
                    file_path, target_id, target_type, mime_type))
                self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(self.client.calls, [])

        oversized = Path(self.temp_dir.name) / "large.bin"
        with oversized.open("wb") as handle:
            handle.truncate(MAX_SINGLE_PART_BYTES + 1)
        result = run(self.handler.upload_file(str(oversized), PAGE_ID))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertIn("single-part", result["message"])
        self.assertEqual(self.client.calls, [])

        prepared, error = FileHandler._prepare_upload(
            str(self.path), PAGE_ID, "page", None)
        self.assertIsNotNone(prepared)
        self.assertIsNone(error)
        assert prepared is not None
        self.assertLessEqual(len(prepared["filename"].encode("utf-8")), MAX_FILENAME_BYTES)

    def test_api_errors_are_propagated_and_failures_are_phase_labeled(self):
        api_error = {"error": True, "code": "FORBIDDEN", "message": "upload disabled"}
        self.client.create_response = api_error
        self.assertIs(run(self.handler.upload_file(str(self.path), PAGE_ID)), api_error)
        self.assertEqual(self.client.calls, [("create", "report.pdf", "application/pdf")])

        self.client = UploadClient()
        self.client.send_response = api_error
        result = run(FileHandler(self.client).upload_file(str(self.path), PAGE_ID))
        self.assertIs(result, api_error)
        self.assertEqual(len(self.client.calls), 2)

        self.client = UploadClient()
        self.client.attach_response = api_error
        result = run(FileHandler(self.client).upload_file(str(self.path), PAGE_ID))
        self.assertIs(result, api_error)
        self.assertEqual(len(self.client.calls), 3)

        self.client = UploadClient()
        self.client.exception = RuntimeError("network down")
        result = run(FileHandler(self.client).upload_file(str(self.path), PAGE_ID))
        self.assertEqual(result["code"], "CREATE_FAILED")
        self.assertNotIn("secret-token", result["message"])


class RecordingUploadHandler:
    def __init__(self):
        self.calls = []

    async def upload_file(self, file_path, target_id, target_type="page", mime_type=None):
        self.calls.append((file_path, target_id, target_type, mime_type))
        return {"ok": True}


class TestFileUploadToolRegistration(unittest.TestCase):
    def setUp(self):
        self.handler = RecordingUploadHandler()
        self.original_ensure_init = server._ensure_init
        self.original_file_handler = server._file_handler
        server._ensure_init = lambda: None
        server._file_handler = self.handler

    def tearDown(self):
        server._ensure_init = self.original_ensure_init
        server._file_handler = self.original_file_handler

    def test_tool_is_registered_and_normalizes_target_id(self):
        names = {tool.name for tool in run(server.mcp.list_tools())}
        self.assertIn("notion_upload_file", names)
        result = run(server.notion_upload_file(
            "/safe/report.pdf", f"https://www.notion.so/Report-{PAGE_ID}",
            "page", "application/pdf"))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.handler.calls, [
            ("/safe/report.pdf", PAGE_ID, "page", "application/pdf")])

    def test_tool_rejects_invalid_target_before_handler(self):
        result = run(server.notion_upload_file("/safe/report.pdf", "not-an-id"))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertEqual(self.handler.calls, [])


if __name__ == "__main__":
    unittest.main()
