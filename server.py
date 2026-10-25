"""notion-agent-kit — MCP Server Entrypoint"""

import os
import asyncio
import logging
from typing import Optional, Any

from mcp.server.fastmcp import FastMCP

from core.notion_client import NotionClient
from core.schema_cache import SchemaCache
from core.block_converter import MarkdownConverter
from core.block_batcher import BlockBatcher
from handlers.comment_handler import CommentHandler
from handlers.file_handler import FileHandler
from handlers.find_handler import FindHandler
from handlers.query_handler import QueryHandler
from handlers.read_handler import ReadHandler
from handlers.user_handler import UserHandler
from handlers.write_handler import WriteHandler
from handlers.validate import extract_page_id, extract_user_id
from core.state_store import SQLiteStateStore

logger = logging.getLogger("notion-agent-kit")

# Initialize MCP server
mcp = FastMCP("notion-agent-kit")

# Global state (initialized on first use)
_client: Optional[NotionClient] = None
_cache: Optional[SchemaCache] = None
_converter: Optional[MarkdownConverter] = None
_batcher: Optional[BlockBatcher] = None
_comment_handler: Optional[CommentHandler] = None
_file_handler: Optional[FileHandler] = None
_find_handler: Optional[FindHandler] = None
_query_handler: Optional[QueryHandler] = None
_read_handler: Optional[ReadHandler] = None
_user_handler: Optional[UserHandler] = None
_write_handler: Optional[WriteHandler] = None
_state_store: Optional[SQLiteStateStore] = None


def _ensure_init():
    global _client, _cache, _converter, _batcher, _comment_handler, _file_handler, _find_handler, _query_handler, _read_handler, _user_handler, _write_handler, _state_store
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
    _comment_handler = CommentHandler(client=_client)
    _file_handler = FileHandler(client=_client)
    _find_handler = FindHandler(client=_client)
    _query_handler = QueryHandler(client=_client)
    _read_handler = ReadHandler(client=_client, max_depth=2)
    _user_handler = UserHandler(client=_client)
    _state_store = SQLiteStateStore.from_env()
    _write_handler = WriteHandler(client=_client, converter=_converter, batcher=_batcher,
                                  cache=_cache, state_store=_state_store)


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


def _resolve_user_id(value: str):
    """Normalize a raw user UUID without treating Notion page URLs as user IDs."""
    user_id = extract_user_id(value)
    if user_id is None:
        return None, {
            "error": True,
            "code": "INVALID_INPUT",
            "message": ("Invalid user ID. Expected 32 hex chars or a UUID; "
                        "Notion page URLs are not user IDs."),
        }
    return user_id, None


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
async def notion_get_self() -> dict:
    """Retrieve the Notion user associated with this token.

    Returns an allowlisted user profile (including safe bot workspace metadata
    when available), never token or raw credential data.
    """
    _ensure_init()
    assert _user_handler is not None
    return await _user_handler.get_self()


@mcp.tool()
async def notion_get_user(user_id: str) -> dict:
    """Retrieve one Notion user, guest, or bot by UUID.

    User IDs accept a dashed UUID or 32 hex characters. A Notion page URL is
    deliberately rejected because the UUID in that URL identifies a page, not a
    user.
    """
    _ensure_init()
    resolved_user_id, err = _resolve_user_id(user_id)
    if err:
        return err
    assert resolved_user_id is not None
    assert _user_handler is not None
    return await _user_handler.get_user(resolved_user_id)


@mcp.tool()
async def notion_get_users(page_size: int = 100,
                           start_cursor: Optional[str] = None) -> dict:
    """List one page of workspace users.

    `page_size` is clamped to 1–100. Pass the opaque `next_cursor` from a
    prior response as `start_cursor`; output preserves `results`, `has_more`,
    and `next_cursor` for stable pagination.
    """
    _ensure_init()
    assert _user_handler is not None
    return await _user_handler.get_users(page_size, start_cursor)


@mcp.tool()
async def notion_get_block_download_urls(block_id: str) -> dict:
    """Get the current direct download URL for a Notion media block.

    Supports image, file, video, pdf, and audio blocks. The response is
    ``{block_id, type, urls}``; each URL entry contains ``url`` and
    ``source`` (``file`` for a Notion-hosted signed URL or ``external``), plus
    ``expiry_time`` when Notion provides one. Retrieve the block again to
    refresh an expired Notion-hosted URL.
    """
    _ensure_init()
    resolved_block_id, err = _resolve_page_id(block_id)
    if err:
        return err
    assert resolved_block_id is not None
    assert _file_handler is not None
    return await _file_handler.get_block_download_urls(resolved_block_id)


@mcp.tool()
async def notion_upload_file(file_path: str, target_id: str,
                             target_type: str = "page",
                             mime_type: Optional[str] = None) -> dict:
    """Upload one local file and attach it as a Notion media block.

    The file must be at most 20 MiB (single-part flow). `target_id` is the
    page/block whose children receive the new file block; `target_type` is
    explicit metadata and may be `page` or `block`. MIME type is inferred from
    the filename unless `mime_type` is supplied.
    """
    _ensure_init()
    resolved_target_id, err = _resolve_page_id(target_id)
    if err:
        return err
    assert resolved_target_id is not None
    assert _file_handler is not None
    return await _file_handler.upload_file(
        file_path, resolved_target_id, target_type=target_type, mime_type=mime_type)


@mcp.tool()
async def notion_create_comment(text: str, page_id: Optional[str] = None,
                                block_id: Optional[str] = None,
                                discussion_id: Optional[str] = None) -> dict:
    """Create a plain-text comment on a page or block, or reply to a discussion.

    Provide exactly one target: `page_id` for a page comment, `block_id` for a
    block comment, or `discussion_id` to reply to an existing discussion.
    """
    _ensure_init()
    if page_id is not None:
        page_id, err = _resolve_page_id(page_id)
        if err:
            return err
    if block_id is not None:
        block_id, err = _resolve_page_id(block_id)
        if err:
            return err
    if discussion_id is not None:
        discussion_id, err = _resolve_page_id(discussion_id)
        if err:
            return err
    assert _comment_handler is not None
    return await _comment_handler.create_comment(
        text, page_id=page_id, block_id=block_id, discussion_id=discussion_id)


@mcp.tool()
async def notion_get_comments(block_id: str, page_size: int = 100) -> dict:
    """List comments attached to a Notion page or block.

    `block_id` may be either a block ID or a page ID. Results include
    `has_more` and `next_cursor` when Notion returns another page.
    """
    _ensure_init()
    block_id, err = _resolve_page_id(block_id)
    if err:
        return err
    assert block_id is not None
    assert _comment_handler is not None
    return await _comment_handler.get_comments(block_id, page_size)


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
async def notion_write_blocks(parent_block_id: str, blocks: list,
                              after: Optional[str] = None) -> dict:
    """Write raw Notion block JSON to a page.
    For block types not supported by markdown (callout, toggle, column, etc.).

    Optional `after` (a sibling block ID) inserts the blocks immediately after
    that block. The block must share the append target's parent; a local guard
    returns INVALID_INPUT (parent mismatch) or NOT_FOUND (block missing).
    Tables must ship with their rows inline under the 'table' object's children,
    otherwise INVALID_INPUT is returned before the request is sent.
    The result includes `verified` (read-back signature match) when the write
    fits in a single batch."""
    _ensure_init()
    parent_block_id, err = _resolve_page_id(parent_block_id)
    if err:
        return err
    if after:
        after, err = _resolve_page_id(after)
        if err:
            return err
    return await _write_handler.write_blocks(parent_block_id, blocks, after=after)


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
                                sorts: Optional[list] = None,
                                start_cursor: Optional[str] = None,
                                fetch_all: bool = False,
                                max_rows: int = 500) -> dict:
    """Query a Notion database and list its rows (pages) with parsed property values.

    Returns each row as: id, url, created, last_edited, and properties (all
    properties parsed to plain values - title/rich_text as str, select/status
    as option name, date as {start,end}, checkbox as bool, etc).

    Pagination:
    - Default: one request, page_size capped at 100. If more rows exist,
      returns has_more=true and next_cursor.
    - start_cursor: continue from a cursor returned by a previous call.
    - fetch_all=true: automatically follow next_cursor until the database is
      exhausted or max_rows (default 500, max 2000) is reached. Returns the
      combined rows plus has_more, next_cursor, total and pages_fetched.

    Filter/sorts use the raw Notion API filter/sort syntax if provided.
    """
    _ensure_init()
    database_id, err = _resolve_page_id(database_id)
    if err:
        return err
    assert database_id is not None
    return await _query_handler.query_database(
        database_id, filter_obj, sorts, page_size,
        start_cursor=start_cursor, fetch_all=fetch_all, max_rows=max_rows)


@mcp.tool()
async def notion_create_database(parent_page_id: str, title: str, properties: dict) -> dict:
    """Create a new database (table) inside a page.

    properties accepts two formats:
    1. Simple: {"Amount": {"type": "number", "format": "rupiah"},
                "Category": {"type": "select", "options": ["Food & Drinks", "Transport"]},
                "Date": {"type": "date"}}
    2. Raw Notion API format: {"Amount": {"number": {"format": "rupiah"}}}

    Supported simple types: title, rich_text, number, select, multi_select,
    status, date, checkbox, url, email, phone_number.
    Number format: any Notion format name (e.g. number, rupiah, dollar, euro).
    Common currency aliases are accepted too and normalized (idr->rupiah,
    usd->dollar, eur->euro, gbp->pound, jpy->yen, sgd->singapore_dollar,
    myr->ringgit, aud->australian_dollar, cad->canadian_dollar, chf->franc,
    cny->yuan, krw->won, inr->rupee, thb->baht). Unknown formats return
    INVALID_INPUT.
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

    Supported property types: title, rich_text, number, select, status,
    multi_select, date (plain string or {"start","end","time_zone"}), checkbox,
    url, email, phone_number, relation (an ID string or a list of IDs /
    {"id": ...} objects). Read-only types (formula, rollup, created_time,
    created_by, last_edited_time, last_edited_by, unique_id) return INVALID_INPUT.
    """
    _ensure_init()
    database_id, err = _resolve_page_id(database_id)
    if err:
        return err
    return await _write_handler.add_database_row(database_id, properties)


@mcp.tool()
async def notion_upsert_database_row(database_id: str, unique_key: str,
                                     unique_value: Any = None,
                                     properties: Optional[dict] = None,
                                     filter_obj: Optional[dict] = None,
                                     expected_fingerprint: Optional[str] = None,
                                     idempotency_key: Optional[str] = None) -> dict:
    """Create or update exactly one database row.

    `unique_key` + `unique_value` build a type-aware exact filter; pass
    `filter_obj` to use a raw Notion filter. Multiple matches return CONFLICT.
    The operation reads before and immediately before update, returns a state
    fingerprint, verifies the read-back, and safely re-queries after an
    ambiguous create failure. `idempotency_key` is process-local only.
    """
    _ensure_init()
    database_id, err = _resolve_page_id(database_id)
    if err:
        return err
    assert database_id is not None
    assert _write_handler is not None
    return await _write_handler.upsert_database_row(
        database_id, unique_key, unique_value, properties, filter_obj,
        expected_fingerprint, idempotency_key)


@mcp.tool()
async def notion_replace_page_content(page_id: str, markdown: Optional[str] = None,
                                      blocks: Optional[list] = None,
                                      expected_fingerprint: Optional[str] = None,
                                      idempotency_key: Optional[str] = None) -> dict:
    """Replace page children using read/diff/apply semantics.

    Use either markdown or raw blocks. Existing raw block IDs are stable
    anchors; unchanged exact-content blocks are retained. The tool updates,
    appends, and deletes with a second read before mutation and verifies final
    content. It refuses unsafe positional first-block insertion/reordering.
    """
    _ensure_init()
    page_id, err = _resolve_page_id(page_id)
    if err:
        return err
    assert page_id is not None
    assert _write_handler is not None
    return await _write_handler.replace_page_content(
        page_id, markdown=markdown, blocks=blocks,
        expected_fingerprint=expected_fingerprint, idempotency_key=idempotency_key)


@mcp.tool()
async def notion_bulk_update_rows(database_id: str, filter_obj: dict,
                                  properties: dict, dry_run: bool = False,
                                  max_rows: int = 100,
                                  expected_fingerprints: Optional[dict] = None,
                                  job_id: Optional[str] = None,
                                  resume: bool = False,
                                  batch_size: int = 100) -> dict:
    """Preview or update bounded rows selected by an explicit filter.

    `dry_run=true` performs reads and returns a preview without PATCH calls.
    The result reports affected, skipped, duplicate, and errored rows. Each
    real update is guarded by a fresh fingerprint and read-back verification.
    """
    _ensure_init()
    database_id, err = _resolve_page_id(database_id)
    if err:
        return err
    assert database_id is not None
    assert _write_handler is not None
    return await _write_handler.bulk_update_rows(
        database_id, filter_obj, properties, dry_run=dry_run,
        max_rows=max_rows, expected_fingerprints=expected_fingerprints,
        job_id=job_id, resume=resume, batch_size=batch_size)


@mcp.tool()
async def notion_get_bulk_job(job_id: str) -> dict:
    """Read durable status for a resumable bulk update job."""
    _ensure_init()
    assert _write_handler is not None
    return _write_handler.get_bulk_job(job_id)


@mcp.tool()
async def notion_resume_bulk_update(job_id: str) -> dict:
    """Resume a paused durable bulk update job from its last checkpoint."""
    _ensure_init()
    assert _write_handler is not None
    return await _write_handler.resume_bulk_job(job_id)


@mcp.tool()
async def notion_append_to_page(page_id: str, markdown: str,
                                after: Optional[str] = None) -> dict:
    """Append markdown content to an existing Notion page.
    Supports: headings, bold, code, lists, tables, dividers, quotes, to-do.

    Optional `after` (a sibling block ID) inserts the new blocks immediately
    after that block (must be a direct child of the page; INVALID_INPUT /
    NOT_FOUND otherwise). The result includes `verified` (read-back signature
    match) when the write fits in a single batch."""
    _ensure_init()
    page_id, err = _resolve_page_id(page_id)
    if err:
        return err
    if after:
        after, err = _resolve_page_id(after)
        if err:
            return err
    return await _write_handler.append_to_page(page_id, markdown, after=after)


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


@mcp.tool()
async def notion_update_page_properties(page_id: str, properties: dict) -> dict:
    """Update properties on an existing page (not a database row).

    The page is fetched first to learn each property's type, then plain values
    are converted with the same rules as notion_add_database_row. Supported
    types: title, rich_text, number, select, status, multi_select, date
    (including ranges as {"start": ..., "end": ...}), checkbox, url, email,
    phone_number, relation. Values already in raw Notion API form
    ({"rich_text": [...]} etc.) are passed through unchanged.

    Example: properties={"Status": "Done", "Due": {"start": "2026-10-01", "end": "2026-10-07"}}

    Unknown property names return INVALID_INPUT listing the properties that
    exist on the page.
    """
    _ensure_init()
    page_id, err = _resolve_page_id(page_id)
    if err:
        return err
    return await _write_handler.update_page_properties(page_id, properties)


@mcp.tool()
async def notion_archive_page(page_id: str, restore: bool = False,
                              object_type: str = "auto") -> dict:
    """Archive a page or database (or restore it with restore=true).

    Idempotent: archiving an already-archived object still succeeds. With
    object_type="auto" (default) a page is tried first and a database is used
    as fallback; pass "page" or "database" to force one. Output:
    {"object": "page"|"database", "id": ..., "archived": true|false}.
    """
    _ensure_init()
    page_id, err = _resolve_page_id(page_id)
    if err:
        return err
    return await _write_handler.archive_page(page_id, restore=restore,
                                             object_type=object_type)


@mcp.tool()
async def notion_move_page(page_id: str, parent_page_id: str) -> dict:
    """Move an existing page beneath another page.

    Both IDs accept a raw UUID or Notion URL. The parent must be a page; this
    tool uses Notion's page-parent update shape and does not use database
    parent semantics. Output: {page_id, parent, response}.
    """
    _ensure_init()
    page_id, err = _resolve_page_id(page_id)
    if err:
        return err
    parent_page_id, err = _resolve_page_id(parent_page_id)
    if err:
        return err
    assert page_id is not None
    assert parent_page_id is not None
    assert _write_handler is not None
    return await _write_handler.move_page(page_id, parent_page_id)


@mcp.tool()
async def notion_duplicate_page(page_id: str, title: Optional[str] = None) -> dict:
    """Duplicate a Notion page (content tree + copyable properties).

    Reads the source page's full block tree (pagination + depth), creates a
    page under the same parent, and appends the blocks in order (tables ship
    with their rows inline). The result is verified by reading back and
    comparing block signatures. `title` overrides the page title; when omitted
    the source title is copied with a ' (copy)' suffix. Read-only properties
    (formula, rollup, created_time, ...) are skipped.

    Output: {page_id, url, blocks_copied, verified, ...}. On a signature
    mismatch `verified` is false with an `explain`; the created page is kept."""
    _ensure_init()
    page_id, err = _resolve_page_id(page_id)
    if err:
        return err
    return await _write_handler.duplicate_page(page_id, title=title)


@mcp.tool()
async def notion_update_table_row(table_row_block_id: str, cells: list) -> dict:
    """Update a table row's cells, preserving existing cell annotations.

    `cells` is a list of cells (one per column). Each cell may be a plain
    string (keeps the old cell's annotations), a dict
    {"text": ..., "annotations": {...}} (explicit annotations), or a full
    rich_text list. The block must be a table_row; otherwise BLOCK_TYPE_MISMATCH.
    Output: {block_id, type: "table_row", cells, duration_ms}."""
    _ensure_init()
    table_row_block_id, err = _resolve_page_id(table_row_block_id)
    if err:
        return err
    return await _write_handler.update_table_row(table_row_block_id, cells)


@mcp.tool()
async def notion_replace_table(table_block_id: str, rows: list) -> dict:
    """Replace a table, allowing a different column count.

    Notion cannot change a table's width in place: a new table is created with
    its rows inline immediately after the old one, then the old table is
    deleted. `rows` is a list of rows; each row is a list of cells (string,
    {"text","annotations"} dict, or rich_text list). All rows must have the same
    column count. Output: {old_table_id, new_table_id, rows, columns}."""
    _ensure_init()
    table_block_id, err = _resolve_page_id(table_block_id)
    if err:
        return err
    return await _write_handler.replace_table(table_block_id, rows)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
