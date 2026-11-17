"""Read handler - read pages, block children, and database schemas."""

import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)


class ReadHandler:
    """Handle notion_read_page and notion_inspect_database tools."""

    def __init__(self, client, max_depth: int = 2):
        self.client = client
        self.max_depth = max_depth

    async def read_page(self, page_id: str, max_blocks: int = 200, depth: int = 2,
                        start_cursor: Optional[str] = None) -> dict:
        if max_blocks < 1:
            max_blocks = 1
        if max_blocks > 1000:
            max_blocks = 1000
        if depth < 1:
            depth = 1
        if depth > 5:
            depth = 5
        if start_cursor is not None and (not isinstance(start_cursor, str) or not start_cursor.strip()):
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "start_cursor must be a non-empty cursor string when provided."}
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
        blocks, has_more, next_cursor, error, pages_fetched = await self._fetch_blocks(
            page_id, max_blocks, depth, start_cursor=start_cursor)
        if error:
            return error
        return {"id": page_id, "title": title, "url": page.get("url", ""),
                "properties": properties, "blocks": blocks, "has_more": has_more,
                "next_cursor": next_cursor, "pages_fetched": pages_fetched}

    async def get_block_children(self, block_id: str, page_size: int = 100,
                                 start_cursor: Optional[str] = None) -> dict:
        """Return one bounded child-block page; pages_fetched is 1, not a provider total."""
        try:
            page_size = max(1, min(int(page_size), 100))
        except (TypeError, ValueError, OverflowError):
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "page_size must be an integer."}
        if start_cursor is not None and (not isinstance(start_cursor, str) or not start_cursor.strip()):
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "start_cursor must be a non-empty cursor string when provided."}
        try:
            response = await self.client.retrieve_block_children(
                block_id, page_size=page_size, start_cursor=start_cursor)
        except Exception as e:
            return {"error": True, "code": "READBACK_INCOMPLETE",
                    "message": f"Could not verify child-block pagination: {e}"}
        if response.get("error"):
            return response
        results = response.get("results")
        has_more = response.get("has_more")
        next_cursor = response.get("next_cursor")
        if not isinstance(results, list) or not isinstance(has_more, bool):
            return {"error": True, "code": "READBACK_INCOMPLETE",
                    "message": "Notion returned an incomplete child-block pagination response."}
        if has_more and (not isinstance(next_cursor, str) or not next_cursor.strip()
                         or next_cursor == start_cursor):
            return {"error": True, "code": "READBACK_INCOMPLETE",
                    "message": "Notion indicated more child blocks without a usable next_cursor."}
        return {"block_id": block_id, "results": results, "has_more": has_more,
                "next_cursor": next_cursor if has_more else None, "pages_fetched": 1}

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

    async def _fetch_blocks(self, block_id: str, max_blocks: int, depth: int,
                            current_depth: int = 0, start_cursor: Optional[str] = None) -> tuple:
        blocks = []
        pages_fetched = 0
        cursor = start_cursor
        seen_cursors = {cursor} if cursor is not None else set()
        while len(blocks) < max_blocks:
            remaining = max_blocks - len(blocks)
            try:
                result = await self.client.retrieve_block_children(
                    block_id, page_size=min(100, remaining), start_cursor=cursor)
            except Exception as e:
                return blocks, False, None, {"error": True, "code": "READBACK_INCOMPLETE",
                                             "message": f"Could not verify block pagination: {e}"}, pages_fetched
            if result.get("error"):
                return blocks, False, None, {"error": True, "code": "READBACK_INCOMPLETE",
                                             "message": result.get("message", "Could not verify block pagination.")}, pages_fetched
            raw_blocks = result.get("results")
            has_more = result.get("has_more")
            next_cursor = result.get("next_cursor")
            if not isinstance(raw_blocks, list) or not isinstance(has_more, bool):
                return blocks, False, None, {"error": True, "code": "READBACK_INCOMPLETE",
                                             "message": "Notion returned an incomplete block pagination response."}, pages_fetched
            if has_more and (not isinstance(next_cursor, str) or not next_cursor.strip()):
                return blocks, False, None, {"error": True, "code": "READBACK_INCOMPLETE",
                                             "message": "Notion indicated more blocks without a usable next_cursor."}, pages_fetched
            if has_more and (not raw_blocks or next_cursor in seen_cursors):
                return blocks, False, None, {"error": True, "code": "READBACK_INCOMPLETE",
                                             "message": "Notion returned an empty page or repeated cursor while more blocks were reported."}, pages_fetched
            if len(raw_blocks) > remaining:
                return blocks, False, None, {"error": True, "code": "READBACK_INCOMPLETE",
                                             "message": "Notion returned more blocks than requested; a safe continuation cursor cannot be proven."}, pages_fetched
            pages_fetched += 1
            for raw in raw_blocks:
                block = self._format_block(raw)
                if block["has_children"] and current_depth < depth - 1:
                    children, child_has_more, _, child_error, child_pages_fetched = await self._fetch_blocks(
                        raw["id"], max_blocks - len(blocks), depth, current_depth + 1)
                    pages_fetched += child_pages_fetched
                    if child_error:
                        return blocks, False, None, child_error, pages_fetched
                    if child_has_more:
                        return blocks, False, None, {"error": True, "code": "READBACK_INCOMPLETE",
                                                     "message": "A nested child list was capped; its continuation cannot be represented by the root cursor."}, pages_fetched
                    block["children"] = children
                blocks.append(block)
            if not has_more:
                return blocks, False, None, None, pages_fetched
            if len(blocks) >= max_blocks:
                return blocks, True, next_cursor, None, pages_fetched
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return blocks, False, None, None, pages_fetched

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
