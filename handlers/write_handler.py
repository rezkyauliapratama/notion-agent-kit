"""Write handler - create pages, write documents, append blocks."""

import logging
import time
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

VALID_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do",
    "toggle", "quote", "callout", "code", "divider",
    "table", "table_row", "image", "bookmark", "embed",
    "link_preview", "column", "column_list", "table_of_contents",
    "synced_block", "template", "link_to_page", "equation",
    "file", "pdf", "video", "audio", "breadcrumb",
}


class WriteHandler:
    """Handle notion_write_document and notion_write_blocks tools."""

    def __init__(self, client, converter, batcher, cache=None):
        self.client = client
        self.converter = converter
        self.batcher = batcher
        self.cache = cache

    async def create_database(self, parent_page_id: str, title: str, properties: dict) -> dict:
        """Create a database with the given schema.

        properties accepts two formats:
        1. Simple: {"Amount": {"type": "number", "format": "idr"},
                     "Category": {"type": "select", "options": ["A", "B"]}}
        2. Raw Notion API format: {"Amount": {"number": {"format": "idr"}}}
        """
        if not title or not title.strip():
            return {"error": True, "code": "INVALID_INPUT", "message": "title is required"}
        if not properties or not isinstance(properties, dict):
            return {"error": True, "code": "INVALID_INPUT", "message": "properties must be a non-empty dict"}
        try:
            api_props = self._build_database_properties(properties)
        except ValueError as e:
            return {"error": True, "code": "INVALID_INPUT", "message": str(e)}
        # Notion requires exactly one title property in every database.
        # Auto-add a default one so callers that forget it don't get a 400.
        if not any(isinstance(v, dict) and "title" in v for v in api_props.values()):
            api_props["Name"] = {"title": {}}
        parent = {"type": "page_id", "page_id": parent_page_id}
        try:
            result = await self.client.create_database(parent, title, api_props)
        except Exception as e:
            return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        db_id = result.get("id", "")
        if self.cache:
            self.cache.set(db_id, result)
        return {
            "database_id": db_id,
            "url": result.get("url", ""),
            "title": title,
            "properties": list(api_props.keys()),
        }

    async def add_database_row(self, database_id: str, properties: dict) -> dict:
        """Add a row (page) to an existing database.

        Values are auto-converted to Notion API format based on the
        database's property schema (fetched + cached). Supports:
        title, rich_text, number, select, status, multi_select, date,
        checkbox, url, email, phone_number.
        """
        if not properties or not isinstance(properties, dict):
            return {"error": True, "code": "INVALID_INPUT", "message": "properties must be a non-empty dict"}
        schema = await self._get_database_schema(database_id)
        if isinstance(schema, dict) and schema.get("error"):
            return schema
        try:
            api_props = self._convert_row_properties(schema, properties)
        except ValueError as e:
            return {"error": True, "code": "INVALID_INPUT", "message": str(e)}
        parent = {"type": "database_id", "database_id": database_id}
        try:
            result = await self.client.create_page(parent, api_props)
        except Exception as e:
            return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        return {
            "page_id": result.get("id", ""),
            "url": result.get("url", ""),
            "properties": list(api_props.keys()),
        }

    # --- Database schema helpers ---

    async def _get_database_schema(self, database_id: str) -> dict:
        if self.cache:
            cached = self.cache.get(database_id)
            if cached:
                return self._normalize_schema(cached.get("properties", {}))
        try:
            result = await self.client.retrieve_database(database_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        if self.cache:
            self.cache.set(database_id, result)
        return self._normalize_schema(result.get("properties", {}))

    @staticmethod
    def _normalize_schema(properties) -> dict:
        """Normalize a database schema to {name: {type: ...}}.

        Fixes a real production bug: the schema cache is shared between
        notion_inspect_database (which stores properties as a LIST of
        {name, type, options}) and notion_add_database_row (which expects a
        DICT keyed by property name). The cached list crashed
        add_database_row with "'list' object has no attribute 'get'".
        """
        if isinstance(properties, dict):
            return properties
        if isinstance(properties, list):
            normalized = {}
            for entry in properties:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                name = entry["name"]
                spec = {"type": entry.get("type", "rich_text")}
                options = entry.get("options")
                if options is not None:
                    spec["options"] = [
                        o.get("name") if isinstance(o, dict) else o for o in options
                    ]
                normalized[name] = spec
            return normalized
        return {}

    @staticmethod
    def _build_database_properties(properties: dict) -> dict:
        """Convert simple schema format to Notion API properties format."""
        SIMPLE_TO_API = {
            "title": "title",
            "rich_text": "rich_text",
            "number": "number",
            "select": "select",
            "multi_select": "multi_select",
            "status": "status",
            "date": "date",
            "checkbox": "checkbox",
            "url": "url",
            "email": "email",
            "phone_number": "phone_number",
        }
        api_props = {}
        for name, spec in properties.items():
            if not isinstance(spec, dict):
                raise ValueError(f"Property '{name}' must be a dict spec, got {type(spec).__name__}")
            # Raw Notion format already has the type as key, e.g. {"number": {...}}
            if any(k in spec for k in ("title", "rich_text", "number", "select",
                                       "multi_select", "status", "date", "checkbox",
                                       "url", "email", "phone_number")):
                api_props[name] = spec
                continue
            ptype = spec.get("type", "rich_text")
            if ptype not in SIMPLE_TO_API:
                raise ValueError(f"Property '{name}': unsupported type '{ptype}'. "
                                 f"Supported: {sorted(SIMPLE_TO_API)}")
            api_key = SIMPLE_TO_API[ptype]
            if ptype == "number":
                fmt = spec.get("format", "number")
                api_props[name] = {"number": {"format": fmt}}
            elif ptype in ("select", "multi_select", "status"):
                options = spec.get("options", [])
                option_objs = [{"name": o} if isinstance(o, str) else o for o in options]
                api_props[name] = {api_key: {"options": option_objs} if option_objs else {}}
            else:
                api_props[name] = {api_key: {}}
        return api_props

    @staticmethod
    def _convert_row_properties(schema: dict, values: dict) -> dict:
        """Convert plain values to Notion API format based on schema types."""
        if not isinstance(schema, dict):
            schema = WriteHandler._normalize_schema(schema)
        converted = {}
        for name, value in values.items():
            prop_spec = schema.get(name, {})
            ptype = prop_spec.get("type") if isinstance(prop_spec, dict) else None
            # Value already in Notion API format (dict with type key)
            if isinstance(value, dict) and any(
                k in value for k in ("title", "rich_text", "number", "select",
                                     "multi_select", "status", "date", "checkbox",
                                     "url", "email", "phone_number")
            ):
                converted[name] = value
                continue
            if ptype == "title" or (ptype is None and name.lower() in ("title", "name", "description") and isinstance(value, str)):
                converted[name] = {"title": [{"type": "text", "text": {"content": str(value)}}]}
            elif ptype == "rich_text":
                converted[name] = {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}
            elif ptype == "number":
                converted[name] = {"number": value}
            elif ptype == "select":
                converted[name] = {"select": {"name": value}}
            elif ptype == "status":
                converted[name] = {"status": {"name": value}}
            elif ptype == "multi_select":
                items = value if isinstance(value, list) else [value]
                converted[name] = {"multi_select": [{"name": i} for i in items]}
            elif ptype == "date":
                converted[name] = {"date": {"start": value}}
            elif ptype == "checkbox":
                converted[name] = {"checkbox": bool(value)}
            elif ptype == "url":
                converted[name] = {"url": value}
            elif ptype == "email":
                converted[name] = {"email": value}
            elif ptype == "phone_number":
                converted[name] = {"phone_number": value}
            elif ptype is None:
                # Schema unknown: fall back to python type heuristics
                if isinstance(value, bool):
                    converted[name] = {"checkbox": value}
                elif isinstance(value, (int, float)):
                    converted[name] = {"number": value}
                elif isinstance(value, list):
                    converted[name] = {"multi_select": [{"name": i} for i in value]}
                else:
                    converted[name] = {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}
            else:
                raise ValueError(f"Property '{name}': unsupported schema type '{ptype}'")
        return converted


    async def write_document(self, parent_page_id: str, title: str,
                              markdown: str, properties: Optional[dict] = None) -> dict:
        start_time = time.monotonic()
        if not markdown or not markdown.strip():
            try:
                parent = {"type": "page_id", "page_id": parent_page_id}
                props = {"title": {"title": [{"type": "text", "text": {"content": title}}]}}
                if properties:
                    props.update(properties)
                page = await self.client.create_page(parent, props)
                if page.get("error"):
                    return page
                return {"page_id": page["id"], "url": page.get("url", ""),
                        "stats": {"total_blocks": 0, "batches": 0, "duration_ms": 0, "rate_limited": False}}
            except Exception as e:
                return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        try:
            blocks = self.converter.convert(markdown)
        except Exception as e:
            return {"error": True, "code": "PARSE_FAILED", "message": f"Failed to parse markdown: {e}"}
        if not blocks:
            try:
                parent = {"type": "page_id", "page_id": parent_page_id}
                props = {"title": {"title": [{"type": "text", "text": {"content": title}}]}}
                if properties:
                    props.update(properties)
                page = await self.client.create_page(parent, props)
                return {"page_id": page["id"], "url": page.get("url", ""),
                        "stats": {"total_blocks": 0, "batches": 0, "duration_ms": 0, "rate_limited": False}}
            except Exception as e:
                return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        try:
            parent = {"type": "page_id", "page_id": parent_page_id}
            props = {"title": {"title": [{"type": "text", "text": {"content": title}}]}}
            if properties:
                props.update(properties)
            first_chunk = blocks[:100]
            remaining = blocks[100:]
            page = await self.client.create_page(parent, props, children=first_chunk)
            if page.get("error"):
                return page
            page_id = page["id"]
        except Exception as e:
            return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        stats = {"total_blocks": len(blocks), "batches": 1, "duration_ms": 0, "rate_limited": False}
        if remaining:
            batch_stats = await self.batcher.append_blocks(page_id, remaining)
            stats["batches"] += batch_stats.get("batches", 0)
            if batch_stats.get("rate_limited"):
                stats["rate_limited"] = True
        stats["duration_ms"] = int((time.monotonic() - start_time) * 1000)
        return {"page_id": page_id, "url": page.get("url", ""), "stats": stats}

    async def write_blocks(self, parent_block_id: str, blocks: List[Dict]) -> dict:
        if not blocks:
            return {"block_count": 0, "batches": 0}
        if len(blocks) > 5000:
            return {"error": True, "code": "TOO_MANY_BLOCKS",
                    "message": "Maximum 5000 blocks per request. Split into multiple calls."}
        for i, block in enumerate(blocks):
            if block.get("type") not in VALID_BLOCK_TYPES:
                return {"error": True, "code": "INVALID_BLOCK",
                        "message": f"Invalid block type '{block.get('type')}' at index {i}. Valid types: {sorted(VALID_BLOCK_TYPES)}"}
        try:
            stats = await self.batcher.append_blocks(parent_block_id, blocks)
        except Exception as e:
            return {"error": True, "code": "APPEND_FAILED", "message": str(e)}
        return {"block_count": len(blocks), "batches": stats.get("batches", 1)}

    async def append_to_page(self, page_id: str, markdown: str) -> dict:
        """Append markdown content to an existing page."""
        start_time = time.monotonic()
        if not markdown or not markdown.strip():
            return {"block_count": 0, "batches": 0, "duration_ms": 0}
        try:
            blocks = self.converter.convert(markdown)
        except Exception as e:
            return {"error": True, "code": "PARSE_FAILED", "message": f"Failed to parse markdown: {e}"}
        if not blocks:
            return {"block_count": 0, "batches": 0, "duration_ms": 0}
        try:
            stats = await self.batcher.append_blocks(page_id, blocks)
        except Exception as e:
            return {"error": True, "code": "APPEND_FAILED", "message": str(e)}
        stats["duration_ms"] = int((time.monotonic() - start_time) * 1000)
        return {"block_count": len(blocks), **stats}

    async def update_block(self, block_id: str, markdown: str) -> dict:
        """Update a block's content from markdown. Works for: paragraph, heading_1-3, quote,
        bulleted_list_item, numbered_list_item, to_do, toggle, callout, code."""
        start_time = time.monotonic()
        if not markdown or not markdown.strip():
            return {"error": True, "code": "INVALID_INPUT", "message": "markdown content is required"}
        try:
            blocks = self.converter.convert(markdown)
        except Exception as e:
            return {"error": True, "code": "PARSE_FAILED", "message": f"Failed to parse markdown: {e}"}
        if not blocks:
            return {"error": True, "code": "PARSE_FAILED", "message": "Markdown produced no blocks"}
        if len(blocks) > 1:
            return {"error": True, "code": "TOO_MANY_BLOCKS",
                    "message": f"Markdown produced {len(blocks)} blocks but notion_update_block updates exactly ONE block. "
                               "Pass a single paragraph/heading/list line, or use notion_append_to_page / notion_write_blocks for multi-block content."}
        block_data = blocks[0]
        block_type = block_data["type"]
        content = {block_type: block_data[block_type]}
        # Notion cannot change a block's type. Fetch the existing block first;
        # if the markdown would change its type, either auto-map plain text onto
        # the existing type (heading/quote/list) or fail with a clear error.
        try:
            existing = await self.client.retrieve_block(block_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if existing.get("error"):
            return existing
        existing_type = existing.get("type", "")
        if existing_type and existing_type != block_type:
            if block_type == "paragraph" and existing_type in (
                "heading_1", "heading_2", "heading_3", "quote",
                "bulleted_list_item", "numbered_list_item", "to_do",
                "toggle", "callout",
            ):
                rich_text = block_data["paragraph"]["rich_text"]
                content = {existing_type: {"rich_text": rich_text}}
                if existing_type == "to_do":
                    content["to_do"]["checked"] = existing.get("to_do", {}).get("checked", False)
                block_type = existing_type
            else:
                return {"error": True, "code": "BLOCK_TYPE_MISMATCH",
                        "message": f"Cannot update block: existing type is '{existing_type}' but markdown converts to '{block_type}'. "
                                   "Notion does not support changing a block's type. Delete + recreate the block, or "
                                   "write markdown that matches the existing type."}
        try:
            result = await self.client.update_block(block_id, content)
        except Exception as e:
            return {"error": True, "code": "UPDATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        duration = int((time.monotonic() - start_time) * 1000)
        return {"block_id": block_id, "type": block_type, "duration_ms": duration}

    async def update_block_raw(self, block_id: str, block_data: List[Dict]) -> dict:
        """Update a block using raw block JSON. Supports all block types including
        callout, toggle, column, synced_block, table, and more."""
        start_time = time.monotonic()
        if not block_data:
            return {"error": True, "code": "INVALID_INPUT", "message": "block_data is required"}
        data = block_data[0] if isinstance(block_data, list) else block_data
        block_type = data.get("type", "")
        if not block_type:
            return {"error": True, "code": "INVALID_INPUT", "message": "block_data must contain a 'type' field"}
        try:
            result = await self.client.update_block(block_id, data)
        except Exception as e:
            return {"error": True, "code": "UPDATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        duration = int((time.monotonic() - start_time) * 1000)
        return {"block_id": block_id, "type": block_type, "duration_ms": duration}

    async def delete_block(self, block_id: str) -> dict:
        """Delete a block by ID. Also removes all child blocks recursively.

        Idempotent: deleting an already-deleted block returns success with
        already_deleted=True instead of an error (observed in production:
        cleanup scripts re-delete blocks and got 404 spam)."""
        start_time = time.monotonic()
        try:
            result = await self.client.delete_block(block_id)
        except Exception as e:
            return {"error": True, "code": "DELETE_FAILED", "message": str(e)}
        if result.get("error"):
            if result.get("code") == "NOT_FOUND":
                return {"deleted": block_id, "already_deleted": True, "duration_ms": 0}
            return result
        duration = int((time.monotonic() - start_time) * 1000)
        return {"deleted": block_id, "duration_ms": duration}
