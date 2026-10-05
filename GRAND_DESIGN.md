# Custom Notion MCP Server - Grand Design

## Enterprise MCP Server Specification

Document ID: BS-ARCH-MCP-001
Version: 1.3
Classification: Internal - Architecture & Engineering
Author: Hermes Agent - Rezky Aulia Pratama
Date: 2026-07-22 (v1.0) / 2026-08-17 (v1.1) / 2026-10-05 (v1.2) / 2026-10-05 (v1.3)
Status: Draft for Review

---

## Executive Summary

Dokumen ini mendefinisikan arsitektur custom MCP (Model Context Protocol) server untuk integrasi Notion yang menggantikan existing MCP Notion tools. Server ini dirancang untuk:

1. **Read** - find, read, query Notion pages dan databases dengan performa tinggi
2. **Write** - create, write, update Notion pages dengan full rich text support (bold, code, tables, lists)
3. **Edge case handling** - rate limit, block limit, concurrent conflict, network failure, unicode, invalid input
4. **Performance** - async I/O, auto-batching, connection pooling, schema caching

---

## 1. Architecture Overview

```
+----------------------------------------------+
|                Hermes Agent                   |
|  +-------------+   +----------------------+  |
|  | Skills       |   | MCP Server Pool     |  |
|  | technical-  |   |  +---------------+  |  |
|  | document-   |   |  | notion        |  |  |
|  | authoring   |   |  | (custom)      |  |  |
|  +-------------+   |  +---------------+  |  |
|                    +----------------------+  |
+----------------------------------------------+
                   |
                   v
      +-------------------------+
      |   Notion API (REST)      |
      |   api.notion.com         |
      +-------------------------+
```

## 2. Directory Structure

```
notion-agent-kit/
+-- server.py                  # MCP entrypoint
+-- requirements.txt           # Python dependencies
+-- README.md
+-- GRAND_DESIGN.md
|
+-- core/
|   +-- notion_client.py       # Raw Notion API wrapper (async httpx)
|   +-- block_converter.py     # Markdown to Notion block JSON converter
|   +-- block_batcher.py       # Split batches, concurrent execution
|   +-- schema_cache.py        # Database schema cache with TTL
|
+-- handlers/
|   +-- find_handler.py        # Search, query, find pages/databases
|   +-- read_handler.py        # Read page, list blocks, query database
|   +-- write_handler.py       # Create page, write document, append, update
|   +-- validate.py            # Input validation, block type validation
|
+-- models/
|   +-- block_types.py         # All Notion block type definitions
|   +-- rich_text.py           # Rich text builder
|   +-- table.py               # Table builder
|   +-- properties.py          # Page property builder
|
+-- utils/
|   +-- retry.py               # Retry with exponential backoff + jitter
|   +-- rate_limiter.py        # Token bucket rate limiter
|   +-- logger.py              # Structured logging
|
+-- tests/
    +-- test_markdown_converter.py
    +-- test_edge_cases.py
    +-- test_performance.py
    +-- test_notion_client.py
```

## 3. Tool Specifications

### notion_find

Search for Notion pages or databases by title.

Input: `query`, `object_type?`, `page_size?`
Output: `{ results: [{ id, title, url, type, parent, last_edited }] }`

### notion_read_page

Read a Notion page's content and metadata.

Input: `page_id`, `max_blocks?` (default 200), `depth?` (default 2)
Output: `{ id, title, url, properties, blocks, has_more, next_cursor }`

### notion_write_document

Write a full document from markdown into a new Notion page. Supports headings, bold, code, lists, tables, dividers, quotes, to-do.

Input: `parent_page_id`, `title`, `markdown`, `properties?`
Output: `{ page_id, url, stats: { total_blocks, batches, duration_ms, rate_limited } }`

### notion_write_blocks

Write raw Notion block JSON to a page. For block types not supported by markdown.

`after?` inserts the blocks immediately after a sibling block (rule 2): the
block is retrieved first and must be a leaf (`has_children false`) or share the
append target's parent, otherwise `INVALID_INPUT`; a missing block returns
`NOT_FOUND`. Only the first batch carries `after`. Tables must ship with their
rows inline under the `table` object's `children` (rule 3) — a table without
inline `table_row` children is rejected locally with `INVALID_INPUT`. The result
includes `verified` (read-back signature match, rule 4) when the write fits in a
single batch.

Input: `parent_block_id`, `blocks[]` (max 5000), `after?`
Output: `{ block_count, batches, verified?, explain? }`

### notion_inspect_database

Inspect a Notion database schema.

Input: `database_id`
Output: `{ id, title, properties: [{ name, type, options? }], data_source_id }`

### notion_create_database

Create a database (table) inside a page. Simple schema format
(`{"Amount": {"type": "number", "format": "rupiah"}}`) or raw Notion API format.
Number-format aliases are normalized before the call (`idr`->`rupiah`,
`usd`->`dollar`, `eur`->`euro`, `gbp`->`pound`, `jpy`->`yen`,
`sgd`->`singapore_dollar`, `myr`->`ringgit`, `aud`->`australian_dollar`,
`cad`->`canadian_dollar`, `chf`->`franc`, `cny`->`yuan`, `krw`->`won`,
`inr`->`rupee`, `thb`->`baht`); an unknown format returns `INVALID_INPUT`
listing the valid values instead of being silently replaced.
Auto-adds a `Name` title property when the caller omits one (Notion requires
exactly one title property per database).

Input: `parent_page_id`, `title`, `properties`
Output: `{ database_id, url, title, properties }`

### notion_query_database

Query a database and list its rows with parsed property values.

Input: `database_id`, `page_size?`, `filter_obj?`, `sorts?`,
`start_cursor?`, `fetch_all?` (default false), `max_rows?` (default 500, max 2000)
Output: `{ database_id, total, has_more, next_cursor, rows }`, plus
`pages_fetched` when `fetch_all=true`.

Default behavior is one request (page_size capped at 100). `start_cursor`
continues from a prior cursor; `fetch_all` follows cursors automatically until
the database is exhausted or `max_rows` is reached.

### notion_add_database_row

Add a row (page) to an existing database. Values are converted to Notion API
format from the database's schema (fetched + cached). Schema is normalized to
a `{name: {type: ...}}` dict regardless of cache format (dict from
retrieve_database or list from inspect_database). Date values accept a plain
string or a range dict `{"start", "end", "time_zone"}` (passed through without
double-wrapping). Relation values accept an ID string, a list of IDs, or
`{"id": ...}` objects. Read-only types (formula, rollup, created_time,
created_by, last_edited_time, last_edited_by, unique_id) return a clear
`INVALID_INPUT`.

Input: `database_id`, `properties`
Output: `{ page_id, url, properties }`

### notion_update_page_properties

Update properties on an existing page. The page is fetched first so each
property's type is known; values are then converted with the same shared logic
as `notion_add_database_row` (no duplicated conversion code). Values already in
raw Notion API form are passed through unchanged. Unknown property names return
`INVALID_INPUT` listing the available properties.

Input: `page_id`, `properties`
Output: `{ page_id, updated: [names], duration_ms }`

### notion_archive_page

Archive (or restore) a page or database. With `object_type="auto"` the pages
endpoint is tried first and a 404 falls back to the databases endpoint; pass
`"page"` or `"database"` to force one. Idempotent: archiving an already
archived object still succeeds.

Input: `page_id`, `restore?` (default false), `object_type?` (auto|page|database)
Output: `{ object: "page"|"database", id, archived, duration_ms }`

### notion_append_to_page

Append markdown content (headings, bold, code, lists, tables, dividers,
quotes, to-do) to an existing page. Batches at 100 blocks. Optional `after?`
inserts the blocks immediately after a sibling block on the page (same guard as
`notion_write_blocks`: `INVALID_INPUT` on parent mismatch, `NOT_FOUND` if the
block is missing). The result includes `verified` (read-back signature match)
when the write fits in a single batch.

Input: `page_id`, `markdown`, `after?`
Output: `{ block_count, batches, duration_ms, rate_limited, verified?, explain? }`

### notion_update_block

Update ONE block's content from markdown. The existing block type is fetched
first: plain-text markdown auto-maps onto heading/quote/list types; any other
type change returns `BLOCK_TYPE_MISMATCH` (Notion does not support type
changes). Multi-block markdown returns `TOO_MANY_BLOCKS`. Raw JSON via
`blocks` parameter for complex types.

Input: `block_id`, `markdown` | `blocks`
Output: `{ block_id, type, duration_ms }`

### notion_delete_block

Delete a block and all children. Idempotent: already-deleted blocks return
`{ deleted, already_deleted: true }` instead of an error (observed in
production cleanup scripts).

Input: `block_id`
Output: `{ deleted, duration_ms }`

### notion_update_table_row

Update a single `table_row`'s cells. The block is retrieved first so each old
cell's annotations can be preserved (rule 6): plain-string cells keep the old
annotations, `{"text": ..., "annotations": {...}}` cells use the explicit
annotations, and a full rich_text list is used as-is. A non-`table_row` block
returns `BLOCK_TYPE_MISMATCH`. The update is `PATCH /v1/blocks/{id}` with
`{"table_row": {"cells": [...]}}`.

Input: `table_row_block_id`, `cells[]`
Output: `{ block_id, type: "table_row", cells, duration_ms }`

### notion_replace_table

Replace a table with one that may have a different column count (rule 5:
Notion cannot change width in place). A new table is created with its rows
inline immediately after the old one (`after` = the old table's ID), then the
old table is deleted. All rows must have the same number of cells; mismatched
rows return `INVALID_INPUT`.

Input: `table_block_id`, `rows[][]`
Output: `{ old_table_id, new_table_id, rows, columns, verified_rows }`

### notion_duplicate_page

Duplicate a page: content tree plus copyable properties. The source block tree
is read fully (pagination + depth), a page is created under the same parent, and
the blocks are appended sequentially (tables carry their rows inline). Block
ordering is preserved and verification compares read-back block signatures
(rule 4) — never result counts. Read-only properties (formula, rollup,
created_time, created_by, last_edited_time, last_edited_by, unique_id) are
skipped; relation values are followed when they carry an id. `title` overrides
the page title; when omitted the source title is copied with a ` (copy)` suffix.
On a signature mismatch the created page is kept and `verified` is false with an
`explain`.

Input: `page_id`, `title?`
Output: `{ page_id, url, blocks_copied, verified, explain? }`

## 4. Block Type Support

| Block Type | Markdown | Raw JSON | v1/v2 |
|-----------|----------|----------|-------|
| paragraph | YES auto | YES | v1 |
| heading_1 | YES `#` | YES | v1 |
| heading_2 | YES `##` | YES | v1 |
| heading_3 | YES `###` | YES | v1 |
| bulleted_list_item | YES `-` | YES | v1 |
| numbered_list_item | YES `1.` | YES | v1 |
| to_do | YES `- [ ]` | YES | v1 |
| code | YES | YES | v1 |
| divider | YES `---` | YES | v1 |
| table | YES `|...|` | YES | v1 |
| table_row | YES (auto) | YES | v1 |
| quote | YES `>` | YES | v1 |
| callout | via raw only | YES | v1 |
| toggle | via raw only | YES | v1 |
| image | via raw only | YES | v1 |
| bookmark | via raw only | YES | v1 |
| embed | via raw only | YES | v1 |
| link_preview | via raw only | YES | v1 |
| breadcrumb | via raw only | YES | v2 |
| column | via raw only | YES | v2 |
| column_list | via raw only | YES | v2 |
| synced_block | via raw only | YES | v2 |
| child_page | via raw only | YES | v2 |
| child_database | via raw only | YES | v2 |
| equation | via raw only | YES | v2 |
| file/pdf/video/audio | via raw only | YES | v2 |

## 5. Edge Case Handling

| Edge Case | Handling |
|-----------|----------|
| Rate limit (429) | Token bucket + exponential backoff + jitter, max 3 retries |
| Block limit (100) | Auto-split into chunks of 100, sent **sequentially** to guarantee block order (2026-10-05) |
| `after` parent mismatch (rule 2) | `after` block must be a leaf or share the target's parent; guarded locally with `INVALID_INPUT` / `NOT_FOUND`; only the first batch carries `after` |
| Table without inline rows (rule 3) | Rejected locally with `INVALID_INPUT` before the request; tables must ship with their `table_row` children |
| Append result count unreliable (rule 4) | Verification reads the page tail back and compares block signatures; never `len(results) == len(sent)` |
| Table width immutable (rule 5) | `notion_replace_table`: create the new table with rows inline right after the old one, then delete the old table |
| Table row annotations lost (rule 6) | `notion_update_table_row` retrieves the block first and preserves per-cell annotations unless explicitly overridden |
| Table width immutable | Calculate width from header BEFORE create |
| Concurrent conflict (409) | Retry with fresh page version |
| Network failure | Exponential backoff: 1s, 2s, 4s |
| Invalid markdown | Graceful degradation, fallback to paragraph |
| Unicode + emoji | UTF-8 throughout |
| Empty content | Filter empty blocks, skip |
| Token expired (401) | Clear error message |
| No access (403) | Clear error message |

## 6. Performance Targets

| Operation | Target |
|-----------|--------|
| notion_find | <1s |
| notion_read_page (200 blocks) | <2s |
| notion_inspect_database (cached) | <50ms |
| notion_write_document (100 blocks) | <3s |
| notion_write_document (500 blocks) | <8s |
| notion_write_document (2000 blocks) | <30s |

## 7. Dependencies

```
mcp>=1.0.0
httpx>=0.28.0
pydantic>=2.0
```

## 8. Error Response Format

```json
{
  "error": true,
  "code": "RATE_LIMITED",
  "message": "Rate limited by Notion API. Retry after 3 seconds.",
  "retry_after": 3
}
```

Error codes: UNAUTHORIZED, FORBIDDEN, NOT_FOUND, CONFLICT, RATE_LIMITED, INVALID_INPUT, NETWORK_ERROR, INTERNAL_ERROR, INVALID_BLOCK

## 9. Known Issues & Fixes (from production)

| Issue | Fix | Date |
|-------|-----|------|
| Block order scrambled for documents >100 blocks (104-block doc landed as 100 + 4 reversed) | `BlockBatcher.append_blocks` sends batches sequentially by default; opt-in `concurrent=True` warns it does not guarantee order | 2026-10-05 |
| `notion_create_database` number format `idr` rejected with 400 `validation_error` | `_resolve_number_format()` maps currency aliases to Notion names (idr->rupiah, ...); unknown formats return `INVALID_INPUT` listing valid values | 2026-10-05 |
| `notion_add_database_row` date range `{"start","end"}` rejected with 400 `start should be a string` | `_normalize_date_value()` passes an existing dict through untouched, drops empty keys, supports `time_zone`; strings unchanged | 2026-10-05 |
| `notion_add_database_row` relation property raised `unsupported schema type 'relation'` | Relation values convert from ID string / list of IDs / `{"id": ...}` into `{"relation": [{"id": ...}]}`; empty clears | 2026-10-05 |
| Read-only property types raised a bare `ValueError` | Return a clear `INVALID_INPUT` naming the type as read-only | 2026-10-05 |
| Append verb confusion (rule 1) | Append uses `PATCH /v1/blocks/{id}/children`; POST is never used | 2026-10-05 |
| `after` block from another parent / missing (rule 2) | `_validate_after` retrieves the block, requires `has_children false` or a matching parent (`INVALID_INPUT`), and returns `NOT_FOUND` when absent; first batch only | 2026-10-05 |
| Table sent without rows (rule 3) | `find_table_missing_children()` rejects tables lacking inline rows (top-level and nested) before the request | 2026-10-05 |
| Append results count unreliable (rule 4) | Verification compares read-back block signatures; result counts are never asserted | 2026-10-05 |
| Table column count cannot change in place (rule 5) | `notion_replace_table` creates the new table after the old one, then deletes the old one | 2026-10-05 |
| Table row update lost annotations (rule 6) | `notion_update_table_row` preserves per-cell annotations from the retrieved block | 2026-10-05 |

---

*End of Document - BS-ARCH-MCP-001 v1.3*
