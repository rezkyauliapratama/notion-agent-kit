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

## Tools (16)

| Tool | Description |
|------|-------------|
| `notion_find` | Search pages/databases by title (`query`, `object_type`, `page_size`) |
| `notion_read_page` | Read page content + metadata (`page_id`, `max_blocks` default 200, `depth`). Returns `has_more` when truncated — re-call with larger `max_blocks` (max 1000) to page through |
| `notion_write_document` | Write a full document from markdown into a new page (headings, bold, code, lists, tables, dividers, quotes, to-do) |
| `notion_write_blocks` | Write raw Notion block JSON (callout, toggle, column, synced_block, ...). Optional `after` inserts after a sibling block. Tables must ship with rows inline |
| `notion_inspect_database` | Inspect database schema (properties, types, options) |
| `notion_create_database` | Create a database inside a page. Simple or raw property schema; auto-adds a `Name` title property if missing; number-format aliases normalized (`idr`→`rupiah`) |
| `notion_add_database_row` | Add a row to a database; values auto-converted from the schema (incl. date ranges and relation) |
| `notion_append_to_page` | Append markdown content to an existing page. Optional `after` inserts after a sibling block |
| `notion_update_block` | Update ONE block from markdown (type-preserving; plain text auto-maps onto heading/quote/list) or raw JSON via `blocks` |
| `notion_delete_block` | Delete a block + children. Idempotent: already-deleted blocks return `already_deleted: true` |
| `notion_update_table_row` | Update a table row's cells (`table_row_block_id`, `cells`). Plain-string cells keep the existing per-cell annotations; `{"text","annotations"}` or rich_text lists override. Non-`table_row` → `BLOCK_TYPE_MISMATCH` |
| `notion_replace_table` | Replace a table with a possibly different column count (`table_block_id`, `rows`): creates the new table (rows inline) right after the old one, then deletes the old table. All rows must have equal column count |
| `notion_duplicate_page` | Duplicate a page (`page_id`, optional `title`): copies the full block tree (pagination + depth) and writable properties, then verifies by read-back signature. Output `{page_id, url, blocks_copied, verified}` |
| `notion_update_page_properties` | Update typed properties on an existing page (`properties`). Fetches the page to learn each property's type, then converts with the same rules as `notion_add_database_row` |
| `notion_archive_page` | Archive/restore a page or database (`restore`, `object_type` auto/page/database). Idempotent |
| `notion_query_database` | Query a database. Pagination params: `start_cursor`, `fetch_all` (follows cursors), `max_rows` (default 500, max 2000) |

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
| `notion_create_database` with number format `idr` → 400 `validation_error` (Notion only accepts its own format names such as `rupiah`) | `_resolve_number_format()` maps currency aliases (idr→rupiah, usd→dollar, eur→euro, gbp→pound, jpy→yen, sgd→singapore_dollar, myr→ringgit, aud→australian_dollar, cad→canadian_dollar, chf→franc, cny→yuan, krw→won, inr→rupee, thb→baht); unknown formats return `INVALID_INPUT` listing valid values (2026-10-05) |
| `notion_add_database_row` with a date range `{"start": "2026-10-01", "end": "2026-10-07"}` → 400 `start should be a string` (the dict was wrapped a second time as `{"start": {dict}}`) | `_normalize_date_value()` passes an existing dict through untouched, drops empty/None keys and supports `time_zone`; plain strings keep the old behavior (2026-10-05) |
| `notion_add_database_row` on a relation property → `INVALID_INPUT "unsupported schema type 'relation'"` | Relation values convert from an ID string, a list of IDs, or `{"id": ...}` dicts into `{"relation": [{"id": ...}]}`; empty input clears the relation (2026-10-05) |
| Read-only property types (formula, rollup, created_time, created_by, last_edited_time, last_edited_by, unique_id) hit an unhelpful `ValueError` | Return a clear `INVALID_INPUT` stating the type is read-only (2026-10-05) |
| Documents over 100 blocks had scrambled order: the trailing batch landed at the top (104-block doc arrived as 100 + 4 reversed) because batches ran in concurrent waves | `BlockBatcher.append_blocks` now sends batches **sequentially** by default, guaranteeing input order. An opt-in `concurrent=True` mode remains but does not guarantee order and warns in its docstring (2026-10-05) |
| Append returned `400 invalid_request_url` when sent as POST | Append uses `PATCH /v1/blocks/{id}/children`; the verb is documented and unchanged (**rule 1**) (2026-10-05) |
| `after` block from a different parent → Notion 400 / silent misplacement | Local guard (`_validate_after`) retrieves the block first and requires `has_children false` or a matching parent; otherwise `INVALID_INPUT`, and a missing block returns `NOT_FOUND` (**rule 2**) (2026-10-05) |
| Table sent without rows → `body.children[N].table.children should be defined` | `find_table_missing_children()` rejects tables lacking inline `table_row` children in `notion_write_blocks` / markdown paths, both top-level and nested (**rule 3**) (2026-10-05) |
| Append responses can contain MORE results than blocks sent; `len(results) == len(sent)` wrongly reported failure | Verification compares block signatures (type + text) by reading the page tail back, never result counts. Skipped (with a note) when the write spans >1 batch (**rule 4**) (2026-10-05) |
| Changing a table's column count in place is impossible | `notion_replace_table` creates a new table (rows inline) right after the old one, then deletes the old table (**rule 5**) (2026-10-05) |
| Updating a table row lost per-cell annotations | `notion_update_table_row` retrieves the block first and preserves each cell's annotations unless the caller supplies explicit ones (**rule 6**) (2026-10-05) |
| Duplicating a page had no first-class tool | `notion_duplicate_page` copies the block tree + writable properties and verifies by read-back signature; read-only properties are skipped (2026-10-05) |

## Notion API notes

- `Notion-Version: 2022-06-28` (stable, battle-tested). Newer versions rename databases to "data sources" in search responses — not adopted to keep `notion_find` filtering stable.
- Rate limit ~3 req/s handled by token bucket + retry/backoff (see `utils/rate_limiter.py`, `utils/retry.py`).
- 100-block append limit handled by `core/block_batcher.py` (batches of 100, sent sequentially with 0.5s between batches to preserve order and respect the ~3 req/s limit).
