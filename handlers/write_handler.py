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

    def __init__(self, client, converter, batcher):
        self.client = client
        self.converter = converter
        self.batcher = batcher

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
