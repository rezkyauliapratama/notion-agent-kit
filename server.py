"""notion-agent-kit — MCP Server Entrypoint"""

import os
import asyncio
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

from core.notion_client import NotionClient
from core.schema_cache import SchemaCache
from core.block_converter import MarkdownConverter
from core.block_batcher import BlockBatcher
from handlers.find_handler import FindHandler
from handlers.read_handler import ReadHandler
from handlers.write_handler import WriteHandler
from handlers.validate import validate_page_id

logger = logging.getLogger("notion-agent-kit")

# Initialize MCP server
mcp = FastMCP("notion-agent-kit")

# Global state (initialized on first use)
_client: Optional[NotionClient] = None
_cache: Optional[SchemaCache] = None
_converter: Optional[MarkdownConverter] = None
_batcher: Optional[BlockBatcher] = None
_find_handler: Optional[FindHandler] = None
_read_handler: Optional[ReadHandler] = None
_write_handler: Optional[WriteHandler] = None


def _ensure_init():
    global _client, _cache, _converter, _batcher, _find_handler, _read_handler, _write_handler
    if _client is not None:
        return

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise ValueError(
            "NOTION_TOKEN environment variable is required. "
            "Set it in .env or export NOTION_TOKEN=ntn_..."
        )

    _client = NotionClient(token=token)
    _cache = SchemaCache()
    _converter = MarkdownConverter()
    _batcher = BlockBatcher(client=_client)
    _find_handler = FindHandler(client=_client)
    _read_handler = ReadHandler(client=_client, max_depth=2)
    _write_handler = WriteHandler(client=_client, converter=_converter, batcher=_batcher)


@mcp.tool()
async def notion_find(query: str, object_type: Optional[str] = None, page_size: int = 10) -> dict:
    """Search for Notion pages or databases by title."""
    _ensure_init()
    return await _find_handler.find(query, object_type, page_size)


@mcp.tool()
async def notion_read_page(page_id: str, max_blocks: int = 200, depth: int = 2) -> dict:
    """Read a Notion page's content and metadata."""
    _ensure_init()
    validation = validate_page_id(page_id)
    if validation:
        return validation
    return await _read_handler.read_page(page_id, max_blocks, depth)


@mcp.tool()
async def notion_write_document(parent_page_id: str, title: str, markdown: str,
                                 properties: Optional[dict] = None) -> dict:
    """Write a full document from markdown into a new Notion page.
    Supports: headings, bold, code, lists, tables, dividers, quotes, to-do."""
    _ensure_init()
    validation = validate_page_id(parent_page_id)
    if validation:
        return validation
    return await _write_handler.write_document(parent_page_id, title, markdown, properties)


@mcp.tool()
async def notion_write_blocks(parent_block_id: str, blocks: list) -> dict:
    """Write raw Notion block JSON to a page.
    For block types not supported by markdown (callout, toggle, column, etc.)."""
    _ensure_init()
    validation = validate_page_id(parent_block_id)
    if validation:
        return validation
    return await _write_handler.write_blocks(parent_block_id, blocks)


@mcp.tool()
async def notion_inspect_database(database_id: str) -> dict:
    """Inspect a Notion database schema (properties, types, options)."""
    _ensure_init()
    validation = validate_page_id(database_id)
    if validation:
        return validation
    cached = _cache.get(database_id)
    if cached:
        return cached
    result = await _read_handler.inspect_database(database_id)
    if "error" not in result:
        _cache.set(database_id, result)
    return result


@mcp.tool()
async def notion_append_to_page(page_id: str, markdown: str) -> dict:
    """Append markdown content to an existing Notion page.
    Supports: headings, bold, code, lists, tables, dividers, quotes, to-do."""
    _ensure_init()
    validation = validate_page_id(page_id)
    if validation:
        return validation
    return await _write_handler.append_to_page(page_id, markdown)


@mcp.tool()
async def notion_update_block(block_id: str, markdown: str) -> dict:
    """Update a Notion block's content from markdown.
    Works for: paragraph, heading_1/2/3, quote, bulleted/numbered list items,
    to_do, toggle, callout, and code blocks."""
    _ensure_init()
    validation = validate_page_id(block_id)
    if validation:
        return validation
    return await _write_handler.update_block(block_id, markdown)


@mcp.tool()
async def notion_delete_block(block_id: str) -> dict:
    """Delete a Notion block by ID. Removes the block and all its children."""
    _ensure_init()
    validation = validate_page_id(block_id)
    if validation:
        return validation
    return await _write_handler.delete_block(block_id)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
