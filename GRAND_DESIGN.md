# Custom Notion MCP Server - Grand Design

## Enterprise MCP Server Specification

Document ID: BS-ARCH-MCP-001
Version: 1.0
Classification: Internal - Architecture & Engineering
Author: Hermes Agent - Rezky Aulia Pratama
Date: 2026-07-22
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

Input: `parent_block_id`, `blocks[]` (max 5000)
Output: `{ block_count, batches }`

### notion_inspect_database

Inspect a Notion database schema.

Input: `database_id`
Output: `{ id, title, properties: [{ name, type, options? }], data_source_id }`

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
| Block limit (100) | Auto-split into chunks of 100, fire concurrently |
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

---

*End of Document - BS-ARCH-MCP-001 v1.0*
