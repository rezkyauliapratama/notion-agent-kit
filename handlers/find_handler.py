"""Find handler - search Notion pages and databases."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class FindHandler:
    """Handle notion_find tool."""

    def __init__(self, client):
        self.client = client

    async def find(self, query: str, object_type: Optional[str] = None, page_size: int = 10) -> dict:
        if page_size < 1:
            page_size = 1
        if page_size > 50:
            page_size = 50
        filter_obj = None
        if object_type in ("page", "database"):
            filter_obj = {"property": "object", "value": object_type}
        try:
            result = await self.client.search(query=query, filter_obj=filter_obj, page_size=page_size)
        except Exception as e:
            return {"error": True, "code": "SEARCH_FAILED", "message": str(e)}
        results = []
        for item in result.get("results", []):
            obj_type = item.get("object", "unknown")
            title = ""
            if obj_type == "page":
                title_prop = item.get("properties", {}).get("title", {})
                title = self._extract_title(title_prop)
            elif obj_type == "database":
                title = "".join(t.get("plain_text", "") for t in item.get("title", []))
            results.append({"id": item.get("id", ""), "title": title or "Untitled",
                            "url": item.get("url", ""), "type": obj_type,
                            "parent": item.get("parent", {}), "last_edited": item.get("last_edited_time", "")})
        return {"results": results}

    def _extract_title(self, title_prop: dict) -> str:
        title_type = title_prop.get("type", "")
        texts = title_prop.get("title", []) if title_type == "title" else (title_prop if isinstance(title_prop, list) else [])
        return "".join(t.get("plain_text", "") for t in texts)
