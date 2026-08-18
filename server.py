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
from handlers.query_handler import QueryHandler
from handlers.read_handler import ReadHandler
from handlers.write_handler import WriteHandler
from handlers.validate import extract_page_id

logger = logging.getLogger("notion-agent-kit")

# Initialize MCP server
mcp = FastMCP("notion-agent-kit")

# Global state (initialized on first use)
_client: Optional[NotionClient] = None
_cache: Optional[SchemaCache] = None
_converter: Optional[MarkdownConverter] = None
_batcher: Optional[BlockBatcher] = None
_find_handler: Optional[FindHandler] = None
_query_handler: Optional[QueryHandler] = None
_read_handler: Optional[ReadHandler] = None
_write_handler: Optional[WriteHandler] = None


def _ensure_init():
    global _client, _cache, _converter, _batcher, _find_handler, _query_handler, _read_handler, _write_handler
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
    _query_handler = QueryHandler(client=_client)
    _read_handler = ReadHandler(client=_client, max_depth=2)
    _write_handler = WriteHandler(client=_client, converter=_converter, batcher=_batcher, cache=_cache)


def _resolve_page_id(value: str):
    """Resolve a page/database ID or Notion URL to a normalized 32-hex ID.

    Returns (normalized_id, None) on success, (None, error_dict) on failure.
    Accepting URLs matters: agents routinely paste app.notion.com links, and
    raw UUID validation used to reject them with confusing errors.
    """
    pid = extract_page_id(value)
    if pid is None:
        return None, {"error": True, "code": "INVALID_INPUT",
                      "message": f"Invalid page/database ID. Expected 32 hex chars, a UUID, or a Notion URL. Got: '{value}'"}
    return pid, None


@mcp.tool()
async def notion_find(query: str, object_type: Optional[str] = None, page_size: int = 10) -> dict:
    """Search for Notion pages or databases by title."""
    _ensure_init()
    return await _find_handler.find(query, object_type, page_size)


@mcp.tool()
async def notion_read_page(page_id: str, max_blocks: int = 200, depth: int = 2) -> dict:
    """Read a Notion page's content and metadata. Returns has_more=true when
    max_blocks truncates the page; call again with a larger max_blocks (max 1000)
    to page through the rest."""
    _ensure_init()
    page_id, err = _resolve_page_id(page_id)
    if err:
        return err
    return await _read_handler.read_page(page_id, max_blocks, depth)


@mcp.tool()
async def notion_write_document(parent_page_id: str, title: str, markdown: str,
                                 properties: Optional[dict] = None) -> dict:
    """Write a full document from markdown into a new Notion page.
    Supports: headings, bold, code, lists, tables, dividers, quotes, to-do."""
    _ensure_init()
    parent_page_id, err = _resolve_page_id(parent_page_id)
    if err:
        return err
    return await _write_handler.write_document(parent_page_id, title, markdown, properties)


@mcp.tool()
async def notion_write_blocks(parent_block_id: str, blocks: list) -> dict:
    """Write raw Notion block JSON to a page.
    For block types not supported by markdown (callout, toggle, column, etc.)."""
    _ensure_init()
    parent_block_id, err = _resolve_page_id(parent_block_id)
    if err:
        return err
    return await _write_handler.write_blocks(parent_block_id, blocks)


@mcp.tool()
async def notion_inspect_database(database_id: str) -> dict:
    """Inspect a Notion database schema (properties, types, options)."""
    _ensure_init()
    database_id, err = _resolve_page_id(database_id)
    if err:
        return err
    cached = _cache.get(database_id)
    if cached:
        return cached
    result = await _read_handler.inspect_database(database_id)
    if "error" not in result:
        _cache.set(database_id, result)
    return result


@mcp.tool()
async def notion_query_database(database_id: str, page_size: int = 50,
                                filter_obj: Optional[dict] = None,
                                sorts: Optional[list] = None) -> dict:
    """Query a Notion database and list its rows (pages) with parsed property values.

    Returns each row as: id, url, created, last_edited, and properties (all
    properties parsed to plain values - title/rich_text as str, select/status
    as option name, date as {start,end}, checkbox as bool, etc).

    Pagination: if the database has more rows than page_size, returns
    has_more=true and next_cursor; call again to page through. Filter/sorts
    use the raw Notion API filter/sort syntax if provided.
    """
    _ensure_init()
    database_id, err = _resolve_page_id(database_id)
    if err:
        return err
    assert database_id is not None
    return await _query_handler.query_database(database_id, filter_obj, sorts, page_size)


@mcp.tool()
async def notion_create_database(parent_page_id: str, title: str, properties: dict) -> dict:
    """Create a new database (table) inside a page.

    properties accepts two formats:
    1. Simple: {"Amount": {"type": "number", "format": "idr"},
                "Category": {"type": "select", "options": ["Food & Drinks", "Transport"]},
                "Date": {"type": "date"}}
    2. Raw Notion API format: {"Amount": {"number": {"format": "idr"}}}

    Supported simple types: title, rich_text, number (format: number/idr/usd),
    select, multi_select, status, date, checkbox, url, email, phone_number.
    A default 'Name' title property is added automatically when none is provided.
    """
    _ensure_init()
    parent_page_id, err = _resolve_page_id(parent_page_id)
    if err:
        return err
    return await _write_handler.create_database(parent_page_id, title, properties)


@mcp.tool()
async def notion_add_database_row(database_id: str, properties: dict) -> dict:
    """Add a row (page) to an existing database.

    Values are auto-converted to Notion API format based on the database
    schema. Example for a spending tracker:
      properties={"Description": "Kopi", "Amount": 10000,
                  "Date": "2026-08-13", "Category": "Food & Drinks"}

    If the schema is unknown for a property, falls back to type heuristics
    (str -> rich_text, int/float -> number, bool -> checkbox, list -> multi_select).
    """
    _ensure_init()
    database_id, err = _resolve_page_id(database_id)
    if err:
        return err
    return await _write_handler.add_database_row(database_id, properties)


@mcp.tool()
async def notion_append_to_page(page_id: str, markdown: str) -> dict:
    """Append markdown content to an existing Notion page.
    Supports: headings, bold, code, lists, tables, dividers, quotes, to-do."""
    _ensure_init()
    page_id, err = _resolve_page_id(page_id)
    if err:
        return err
    return await _write_handler.append_to_page(page_id, markdown)


@mcp.tool()
async def notion_update_block(block_id: str, markdown: str = "", blocks: Optional[list] = None) -> dict:
    """Update a Notion block's content from markdown.
    Works for: paragraph, heading_1/2/3, quote, bulleted/numbered list items,
    to_do, toggle, callout, and code blocks.

    For complex block types (callout, toggle, table, column, synced_block),
    pass raw block JSON via the 'blocks' parameter instead of markdown.
    Notion cannot change a block's type - plain text is auto-mapped onto the
    existing type; anything else returns a BLOCK_TYPE_MISMATCH error.
    """
    _ensure_init()
    block_id, err = _resolve_page_id(block_id)
    if err:
        return err
    if blocks is not None:
        return await _write_handler.update_block_raw(block_id, blocks)
    return await _write_handler.update_block(block_id, markdown)


@mcp.tool()
async def notion_delete_block(block_id: str) -> dict:
    """Delete a Notion block by ID. Removes the block and all its children.
    Idempotent: deleting an already-deleted block returns already_deleted=true."""
    _ensure_init()
    block_id, err = _resolve_page_id(block_id)
    if err:
        return err
    return await _write_handler.delete_block(block_id)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
