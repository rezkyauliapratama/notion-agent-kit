"""Read handler - read pages, block children, and database schemas."""

import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)


class ReadHandler:
    """Handle notion_read_page and notion_inspect_database tools."""

    def __init__(self, client, max_depth: int = 2):
        self.client = client
        self.max_depth = max_depth

    async def read_page(self, page_id: str, max_blocks: int = 200, depth: int = 2) -> dict:
        if max_blocks < 1:
            max_blocks = 1
        if max_blocks > 1000:
            max_blocks = 1000
        if depth < 1:
            depth = 1
        if depth > 5:
            depth = 5
        try:
            page = await self.client.retrieve_page(page_id)
        except Exception as e:
            return {"error": True, "code": "NOT_FOUND", "message": str(e)}
        if page.get("error"):
            return page
        if page.get("in_trash", False):
            return {"error": True, "code": "IN_TRASH", "message": "Page is in trash. Restore it first."}
        title = self._extract_title(page)
        properties = self._extract_properties(page)
        blocks, has_more = await self._fetch_blocks(page_id, max_blocks, depth)
        return {"id": page_id, "title": title, "url": page.get("url", ""),
                "properties": properties, "blocks": blocks, "has_more": has_more}

    async def inspect_database(self, database_id: str) -> dict:
        try:
            database = await self.client.retrieve_database(database_id)
        except Exception as e:
            return {"error": True, "code": "NOT_FOUND", "message": str(e)}
        if database.get("error"):
            return database
        raw_properties = database.get("properties", {})
        properties = []
        for name, prop in raw_properties.items():
            entry = {"name": name, "type": prop.get("type", "unknown")}
            prop_type = prop.get("type", "")
            if prop_type in ("select", "multi_select", "status"):
                options_data = prop.get(prop_type, {}).get("options", [])
                entry["options"] = [{"id": o.get("id"), "name": o.get("name"), "color": o.get("color")} for o in options_data]
            properties.append(entry)
        data_sources = database.get("data_sources", [])
        data_source_id = data_sources[0].get("id", "") if data_sources else ""
        return {"id": database_id, "title": "".join(t.get("plain_text", "") for t in database.get("title", [])),
                "properties": properties, "data_source_id": data_source_id}

    async def _fetch_blocks(self, block_id: str, max_blocks: int, depth: int, current_depth: int = 0) -> tuple:
        blocks = []
        has_more = False
        cursor = None
        while len(blocks) < max_blocks:
            try:
                result = await self.client.retrieve_block_children(block_id, page_size=100, start_cursor=cursor)
            except Exception:
                break
            if result.get("error"):
                break
            raw_blocks = result.get("results", [])
            if not raw_blocks:
                break
            for raw in raw_blocks:
                if len(blocks) >= max_blocks:
                    has_more = True
                    break
                block = self._format_block(raw)
                if block["has_children"] and current_depth < depth - 1 and len(blocks) < max_blocks:
                    children, _ = await self._fetch_blocks(raw["id"], max_blocks - len(blocks), depth, current_depth + 1)
                    block["children"] = children
                blocks.append(block)
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")
        return blocks, has_more

    def _format_block(self, raw: dict) -> dict:
        block_type = raw.get("type", "unknown")
        content = raw.get(block_type, {})
        rich_text = content.get("rich_text", [])
        plain_text = "".join(t.get("plain_text", "") for t in rich_text)
        return {"id": raw.get("id", ""), "type": block_type, "text": plain_text,
                "rich_text": rich_text, "has_children": raw.get("has_children", False)}

    def _extract_title(self, page: dict) -> str:
        for _, prop in page.get("properties", {}).items():
            if prop.get("type") == "title":
                return "".join(t.get("plain_text", "") for t in prop.get("title", []))
        return "Untitled"

    def _extract_properties(self, page: dict) -> list:
        result = []
        for name, prop in page.get("properties", {}).items():
            result.append({"name": name, "type": prop.get("type", "unknown"), "value": self._extract_property_value(prop)})
        return result

    def _extract_property_value(self, prop: dict) -> Any:
        prop_type = prop.get("type", "")
        data = prop.get(prop_type, {})
        if prop_type == "rich_text":
            return "".join(t.get("plain_text", "") for t in data)
        elif prop_type == "title":
            return "".join(t.get("plain_text", "") for t in data)
        elif prop_type == "number":
            return data
        elif prop_type == "select":
            return data.get("name") if data else None
        elif prop_type == "multi_select":
            return [o.get("name") for o in data] if data else []
        elif prop_type == "date":
            return {"start": data.get("start"), "end": data.get("end")} if data else None
        elif prop_type == "checkbox":
            return bool(data)
        elif prop_type in ("url", "email", "phone_number"):
            return data
        elif prop_type == "status":
            return data.get("name") if data else None
        return str(data) if data else None
