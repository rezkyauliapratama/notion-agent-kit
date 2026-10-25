"""Notion REST API wrapper - async httpx with rate limiting and retry."""

import logging
from typing import Optional

import httpx

from utils.rate_limiter import RateLimiter
from utils.retry import with_retry

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1/"
NOTION_VERSION = "2022-06-28"


class NotionClient:
    """Async Notion API client with rate limiting and retry."""

    # The 2022-06-28 children endpoint documents `after` as a sibling
    # insertion primitive. Handlers require this explicit capability marker
    # before attempting an insertion that depends on provider ordering.
    supports_ordered_after = True

    def __init__(self, token: str):
        self.token = token
        self.rate_limiter = RateLimiter(max_rps=3)
        self._client = httpx.AsyncClient(
            base_url=NOTION_API_BASE,
            # Let httpx select the JSON content type for ``json=`` requests
            # and generate a boundary for multipart ``files=`` requests. A
            # client-wide Content-Type would incorrectly label uploads as JSON.
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
            },
            timeout=httpx.Timeout(5.0, connect=5.0, read=30.0, write=30.0),
            limits=httpx.Limits(max_keepalive_connections=10),
        )

    async def close(self):
        await self._client.aclose()

    @with_retry(max_retries=3)
    async def _request(self, method: str, path: str, **kwargs) -> dict:
        await self.rate_limiter.acquire()
        try:
            files = kwargs.get("files")
            if isinstance(files, dict):
                # ``_request`` is retried by ``with_retry``. Rewind file
                # handles before each attempt so a timeout does not retry with
                # an exhausted multipart stream.
                for file_spec in files.values():
                    if isinstance(file_spec, tuple) and len(file_spec) > 1:
                        file_handle = file_spec[1]
                        if hasattr(file_handle, "seek"):
                            file_handle.seek(0)
            response = await self._client.request(method, path, **kwargs)
            return await self._handle_response(response)
        except httpx.TimeoutException:
            logger.warning(f"Request timeout: {method} {path}")
            raise
        except httpx.ConnectError:
            logger.warning(f"Connection error: {method} {path}")
            raise

    async def _handle_response(self, response: httpx.Response) -> dict:
        if response.status_code == 200:
            return response.json()
        error_map = {400: "INVALID_INPUT", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
                     404: "NOT_FOUND", 409: "CONFLICT", 429: "RATE_LIMITED"}
        status = response.status_code
        code = error_map.get(status, "INTERNAL_ERROR")
        body = response.text
        logger.error(f"Notion API error {status}: {body}")
        if status == 429:
            retry_after = int(response.headers.get("Retry-After", 3))
            return {"error": True, "code": code, "message": f"Rate limited. Retry after {retry_after}s.", "retry_after": retry_after}
        messages = {
            "UNAUTHORIZED": "Notion token invalid. Check NOTION_TOKEN in .env",
            "FORBIDDEN": "Integration does not have access. Share the page with the integration first.",
            "NOT_FOUND": "Page/database not found. Verify the ID format (32 hex chars, 8-4-4-4-12).",
            "CONFLICT": "Page is being edited by another session. Try again.",
            "INVALID_INPUT": f"Invalid request: {body}",
            "INTERNAL_ERROR": f"Notion server error: {body}",
        }
        return {"error": True, "code": code, "message": messages.get(code, f"Unknown error: {body}")}

    # --- Read ---
    async def search(self, query: str = "", filter_obj: Optional[dict] = None, page_size: int = 10) -> dict:
        body = {"query": query, "page_size": min(page_size, 50)}
        if filter_obj:
            body["filter"] = filter_obj
        return await self._request("POST", "search", json=body)

    async def retrieve_page(self, page_id: str) -> dict:
        return await self._request("GET", f"pages/{page_id}")

    async def retrieve_block_children(self, block_id: str, page_size: int = 100, start_cursor: Optional[str] = None) -> dict:
        params = {"page_size": min(page_size, 100)}
        if start_cursor:
            params["start_cursor"] = start_cursor
        return await self._request("GET", f"blocks/{block_id}/children", params=params)

    async def retrieve_block(self, block_id: str) -> dict:
        return await self._request("GET", f"blocks/{block_id}")

    async def retrieve_database(self, database_id: str) -> dict:
        return await self._request("GET", f"databases/{database_id}")

    async def query_database(self, database_id: str, filter_obj: Optional[dict] = None,
                              sorts: Optional[list] = None, page_size: int = 100,
                              start_cursor: Optional[str] = None) -> dict:
        body: dict = {"page_size": min(page_size, 100)}
        if filter_obj:
            body["filter"] = filter_obj
        if sorts:
            body["sorts"] = sorts
        if start_cursor:
            body["start_cursor"] = start_cursor
        return await self._request("POST", f"databases/{database_id}/query", json=body)

    # --- Users ---
    async def retrieve_user(self, user_id: str) -> dict:
        return await self._request("GET", f"users/{user_id}")

    async def list_users(self, page_size: int = 100,
                         start_cursor: Optional[str] = None) -> dict:
        params: dict = {"page_size": min(page_size, 100)}
        if start_cursor is not None:
            params["start_cursor"] = start_cursor
        return await self._request("GET", "users", params=params)

    async def get_self(self) -> dict:
        return await self._request("GET", "users/me")

    # --- Write ---
    async def create_page(self, parent: dict, properties: dict, children: Optional[list] = None) -> dict:
        body = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children
        return await self._request("POST", "pages", json=body)

    async def create_database(self, parent: dict, title: str, properties: dict) -> dict:
        body = {
            "parent": parent,
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        }
        return await self._request("POST", "databases", json=body)

    async def append_block_children(self, block_id: str, children: list,
                                    after: Optional[str] = None) -> dict:
        """Append children to a block/page.

        IMPORTANT (production rule 1): appending uses PATCH /v1/blocks/{id}/children.
        POST is answered with a misleading 400 "invalid_request_url". Do not
        change this verb.

        `after` is a sibling block ID; when set, Notion inserts the children
        immediately after it. The block is forwarded as-is (the API validates
        that `after` shares the same parent as `block_id`).
        """
        body: dict = {"children": children}
        if after:
            body["after"] = after
        return await self._request("PATCH", f"blocks/{block_id}/children", json=body)

    async def update_block(self, block_id: str, block_data: dict) -> dict:
        return await self._request("PATCH", f"blocks/{block_id}", json=block_data)

    async def update_page_properties(self, page_id: str, properties: dict) -> dict:
        return await self._request("PATCH", f"pages/{page_id}", json={"properties": properties})

    async def move_page(self, page_id: str, parent_page_id: str) -> dict:
        """Move a page beneath another page using Notion's move endpoint."""
        return await self._request(
            "POST", f"pages/{page_id}/move",
            json={"parent": {"page_id": parent_page_id}},
        )

    # --- File uploads ---
    async def create_file_upload(self, filename: str, content_type: str) -> dict:
        """Create a single-part file upload reservation.

        ``mode`` is explicit so this path never accidentally creates a
        multi-part upload that this client cannot complete.
        """
        return await self._request(
            "POST", "file_uploads",
            json={
                "mode": "single_part",
                "filename": filename,
                "content_type": content_type,
            },
        )

    async def send_file_upload(self, file_upload_id: str, file_path,
                               filename: str, content_type: str) -> dict:
        """Send one local file as the upload's multipart ``file`` field.

        The file handle stays open only for the request and is always closed,
        including when httpx or the API raises. ``part_number`` is deliberately
        omitted because this is the single-part flow.
        """
        with open(file_path, "rb") as file_handle:
            return await self._request(
                "POST", f"file_uploads/{file_upload_id}/send",
                files={"file": (filename, file_handle, content_type)},
            )

    async def delete_block(self, block_id: str) -> dict:
        return await self._request("DELETE", f"blocks/{block_id}")

    # --- Archive ---
    async def archive_page(self, object_id: str, archived: bool,
                           object_type: str = "auto") -> dict:
        """Archive or restore a page or database.

        Pages and databases live on different endpoints. With object_type
        "auto" the pages endpoint is tried first; a 404 (the ID is actually a
        database) falls back to the databases endpoint. Idempotent: re-archiving
        an already-archived object returns 200 from Notion.
        """
        error: dict
        if object_type in ("page", "auto"):
            result = await self._request("PATCH", f"pages/{object_id}",
                                         json={"archived": archived})
            if not result.get("error"):
                return result
            if object_type == "page" or result.get("code") != "NOT_FOUND":
                return result
            error = result
        else:
            error = {"error": True, "code": "INVALID_INPUT",
                     "message": f"object_type must be 'page', 'database' or 'auto', got '{object_type}'"}
        if object_type in ("database", "auto"):
            return await self._request("PATCH", f"databases/{object_id}",
                                       json={"archived": archived})
        return error

    # --- Comments ---
    async def retrieve_comments(self, block_id: str, page_size: int = 100) -> dict:
        return await self._request("GET", "comments", params={"block_id": block_id, "page_size": min(page_size, 100)})

    async def create_comment(self, rich_text: list, parent: Optional[dict] = None, discussion_id: Optional[str] = None) -> dict:
        body = {"rich_text": rich_text}
        if parent:
            body["parent"] = parent
        if discussion_id:
            body["discussion_id"] = discussion_id
        return await self._request("POST", "comments", json=body)
