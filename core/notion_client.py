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

    def __init__(self, token: str):
        self.token = token
        self.rate_limiter = RateLimiter(max_rps=3)
        self._client = httpx.AsyncClient(
            base_url=NOTION_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0),
            limits=httpx.Limits(max_keepalive_connections=10),
        )

    async def close(self):
        await self._client.aclose()

    @with_retry(max_retries=3)
    async def _request(self, method: str, path: str, **kwargs) -> dict:
        await self.rate_limiter.acquire()
        try:
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
                              sorts: Optional[list] = None, page_size: int = 100) -> dict:
        body = {"page_size": min(page_size, 100)}
        if filter_obj:
            body["filter"] = filter_obj
        if sorts:
            body["sorts"] = sorts
        return await self._request("POST", f"databases/{database_id}/query", json=body)

    # --- Write ---
    async def create_page(self, parent: dict, properties: dict, children: Optional[list] = None) -> dict:
        body = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children
        return await self._request("POST", "pages", json=body)

    async def append_block_children(self, block_id: str, children: list) -> dict:
        return await self._request("PATCH", f"blocks/{block_id}/children", json={"children": children})

    async def update_block(self, block_id: str, block_data: dict) -> dict:
        return await self._request("PATCH", f"blocks/{block_id}", json=block_data)

    async def update_page_properties(self, page_id: str, properties: dict) -> dict:
        return await self._request("PATCH", f"pages/{page_id}", json={"properties": properties})

    async def delete_block(self, block_id: str) -> dict:
        return await self._request("DELETE", f"blocks/{block_id}")

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
