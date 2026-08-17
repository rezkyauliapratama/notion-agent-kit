# notion-agent-kit

Custom Notion MCP server for Hermes Agent — full read/write support for all Notion block types with enterprise-grade edge case handling and performance.

## Quick Start

```bash
pip install -r requirements.txt
export NOTION_TOKEN="ntn_..."
python server.py
```

Run the test suite:

```bash
python -m unittest discover -s tests -v
```

## Tools (10)

| Tool | Description |
|------|-------------|
| `notion_find` | Search pages/databases by title (`query`, `object_type`, `page_size`) |
| `notion_read_page` | Read page content + metadata (`page_id`, `max_blocks` default 200, `depth`). Returns `has_more` when truncated — re-call with larger `max_blocks` (max 1000) to page through |
| `notion_write_document` | Write a full document from markdown into a new page (headings, bold, code, lists, tables, dividers, quotes, to-do) |
| `notion_write_blocks` | Write raw Notion block JSON (callout, toggle, column, synced_block, ...) |
| `notion_inspect_database` | Inspect database schema (properties, types, options) |
| `notion_create_database` | Create a database inside a page. Simple or raw property schema; auto-adds a `Name` title property if missing |
| `notion_add_database_row` | Add a row to a database; values auto-converted from the schema |
| `notion_append_to_page` | Append markdown content to an existing page |
| `notion_update_block` | Update ONE block from markdown (type-preserving; plain text auto-maps onto heading/quote/list) or raw JSON via `blocks` |
| `notion_delete_block` | Delete a block + children. Idempotent: already-deleted blocks return `already_deleted: true` |

## Accepted ID formats

All `*_id` parameters accept:
- UUID with dashes: `3aa0f42d-7035-802b-bf61-e7effd583a39`
- 32 hex chars: `3aa0f42d7035802bbf61e7effd583a39`
- Full Notion URL: `https://www.notion.so/...-38b0f42d7035808fbeb263f818eb3187` (last 32-hex token wins)

## Design

See [GRAND_DESIGN.md](GRAND_DESIGN.md) for full architecture documentation.

## Known Issues & Fixes (from production)

| Issue | Fix |
|-------|-----|
| `notion_add_database_row` crashed `'list' object has no attribute 'get'` when the database was inspected first (schema cache stored properties as a LIST; row-add expected a DICT) | `_normalize_schema()` in `write_handler.py` normalizes both formats (2026-08-17) |
| `notion_update_block` with plain text on a heading/list block → 400 `Block type mismatch` | Fetch existing block first; plain text auto-maps onto the existing type; other mismatches return `BLOCK_TYPE_MISMATCH` with guidance |
| Rich text / code longer than 2000 chars → 400 `validation_error` | `_split_long_rich_text()` + code-block chunking into multiple rich_text elements |
| Markdown tables with empty middle cells shifted columns | `_split_table_row()` preserves empty-cell positions |
| DELETE on already-deleted blocks → 404 error spam | Idempotent delete returns `already_deleted: true` |
| Passing Notion URLs → `path.block_id should be a valid uuid` | `extract_page_id()` resolves URLs (last 32-hex token) |
| Creating a database without a title property → 400 (Notion requires exactly one) | Auto-adds `Name` title property |

## Notion API notes

- `Notion-Version: 2022-06-28` (stable, battle-tested). Newer versions rename databases to "data sources" in search responses — not adopted to keep `notion_find` filtering stable.
- Rate limit ~3 req/s handled by token bucket + retry/backoff (see `utils/rate_limiter.py`, `utils/retry.py`).
- 100-block append limit handled by `core/block_batcher.py` (batches of 100, waves of 3, 0.5s between waves).
