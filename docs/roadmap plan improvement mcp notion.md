# Notion MCP roadmap — Stage 1–4 implementation status

Date: 2026-10-24
Working copy: `notion-agent-kit` at the delegated working tree (no commit)
Production API version: `2022-06-28` (unchanged)

## Implemented

### Stage 1 — Reliability

- Added additive result metadata to the new high-level workflows:
  `operation`, `changed`, `verified`, state `fingerprint`, and useful operation
  counts. Existing tool response keys and low-level tool contracts were not
  removed or renamed.
- Added optimistic fingerprint guards to upsert, page replacement, and bulk
  update. Each operation reads state, checks an optional caller fingerprint,
  reads again immediately before mutation, and returns `CONFLICT` instead of
  applying a stale update.
- Added read-back verification for created/updated rows and replaced page
  content. Bulk operations report per-row verification and errors.
- Added safe duplicate handling: unique-key upsert refuses multiple matches;
  an ambiguous create exception is re-queried before a retry can create a
  duplicate.
- Kept the Notion API version pinned to `2022-06-28`; no runtime or gateway
  files were modified.

### Stage 2 — Upsert/bulk workflow

- Added `notion_upsert_database_row` with type-aware unique-key filtering or a
  caller-supplied raw filter, create-if-missing, update-if-single,
  duplicate-match `CONFLICT`, read-back verification, and bounded safe retry
  behavior.
- Added `notion_replace_page_content` with markdown or raw recursive blocks,
  read/diff/apply behavior, update/append/delete counts, stable raw-ID and
  normalized content anchors, stale guards, and final verification.
- Added `notion_bulk_update_rows` with mandatory explicit filter, `dry_run`, a
  synchronous 500-row safety bound plus durable resumable pagination above it,
  reporting, and read-back verification.
- Added focused offline tests in `tests/test_stage12_workflows.py` covering
  create/idempotency replay, duplicate conflict, stale fingerprint conflict,
  page update/delete with ID preservation, and dry-run/filter safety.
- Updated `README.md` and `GRAND_DESIGN.md` with tool contracts, reliability
  behavior, anchors, idempotency scope, and limitations.

## Test evidence

The focused workflow test command passed:

```text
.venv/bin/python -m unittest tests.test_stage12_workflows -v
Ran 5 tests in 0.003s
OK
```

The final full suite passed:

```text
.venv/bin/python -m unittest discover -s tests -v
Ran 145 tests in 4.235s
OK
```

Additional final checks passed:

```text
.venv/bin/python -m py_compile $(find core handlers models utils -name '*.py' -print) server.py tests/test_stage12_workflows.py
# exit 0

git diff --check
# exit 0
```

The server registers 29 tools (the baseline 24, three Stage 2 tools, and two
resumable bulk status/resume tools).

## Known limitations and explicit safety choices

- Notion API `2022-06-28` has no conditional PATCH/ETag write primitive. The
  fingerprint cycle is optimistic, not an atomic server-side compare-and-swap;
  a race after the final guard read cannot be eliminated by this client.
- Idempotency keys use the shared SQLite state store with expiry and payload
  hash replay/conflict semantics. They are not a Notion-side idempotency token;
  unique-key matching and ambiguous-create re-query remain safety mechanisms.
- Page replacement recursively diffs child trees and preserves IDs where anchors
  are safe. It refuses unproven reordering/insertion with `ORDERING_UNPROVEN`.
- Anonymous changed markdown blocks cannot be matched to an old block without
  a stable identity; they are treated as delete/create. Raw blocks should carry
  an existing Notion `id` when an update must preserve identity.
- Bulk duplicate reporting can identify duplicate IDs returned by a query. A
  database-level duplicate business key must be handled by upsert's unique-key
  conflict path.
- Existing low-level writes were intentionally not globally reshaped where
  exact legacy response behavior could be affected; the new high-level write
  results use the standardized metadata contract.

## Stage 3 — durability and broader conflict controls — COMPLETE

- Added `core/state_store.py`, a shared SQLite store initialized from
  `NOTION_STATE_DB` (default `~/.cache/notion-agent-kit/state.sqlite3`). It uses
  WAL/short-lived connections, restrictive file permissions, expiry, payload
  hashes, atomic replay/conflict claims, and stores no provider credentials.
- Upsert and page replacement now use durable idempotency. Same key + same hash
  replays across handler instances; same key + different hash returns `CONFLICT`.
- Added optional audit records (`NOTION_AUDIT_ENABLED=1`) with recursive
  credential-key redaction, bounded result size, and retention pruning via
  `NOTION_AUDIT_RETENTION`. Audit failures never change a completed write result.
- Added durable bulk job state and versioned checkpoints. A synchronous bulk
  call remains capped at 500 rows; `max_rows > 500`, `job_id`, or `resume=true`
  uses sequential cursor pagination. `notion_get_bulk_job` and
  `notion_resume_bulk_update` expose local status/resume operations. Each row is
  guarded/read back, and a restart replays only through the fresh guard.
- Added provider-neutral fake clients and offline integration seams. No live
  Notion request is made by the Stage 3/4 tests, and no live fixture is claimed
  without explicit credentials/network policy.
- Kept `Notion-Version: 2022-06-28`. This API has no server-side conditional
  PATCH/ETag/CAS. The optimistic fingerprint guard remains and its residual
  race is documented rather than overstated.

## Stage 4 — richer content and operations — COMPLETE WITH BOUNDED LIMITS

- `notion_replace_page_content` now preflights and recursively diffs nested
  child trees. Stable raw IDs and normalized anchors preserve child identity
  where safe; nested updates/deletes/appends are applied sequentially and the
  complete tree is read back.
- Existing-block reordering and insert-before are not guessed. The implementation
  returns `ORDERING_UNPROVEN` before mutation unless the client explicitly
  advertises the documented ordered `after` capability. NotionClient advertises
  that capability for the API primitive it implements; provider-neutral fakes
  must opt in explicitly.
- Bulk execution is extended beyond 500 through durable cursor jobs, sequential
  scheduling, per-row checkpoints, and the existing provider rate limiter/retry
  path. There is no parallel write scheduler.
- Reviewed legacy update/archive/block result paths and added additive
  `operation`, `changed`, and `verified` metadata while retaining existing keys.
  Where legacy behavior has no read-back contract, `verified` is `null`.
- Remaining genuine limitations are the provider's lack of atomic conditional
  writes, no safe insert-before primitive, and no live Notion integration test
  without credentials/network approval. These are explicit errors/limitations,
  not stubs or silently claimed guarantees.

## Test evidence

Focused Stage 1/2 and Stage 3/4 tests:

```text
.venv/bin/python -m unittest tests.test_stage12_workflows tests.test_stage34_workflows -v
Ran 12 tests in 1.093s
OK
```

Full unittest suite and final static checks:

```text
.venv/bin/python -m unittest discover -s tests -v
Ran 152 tests in 5.411s
OK

.venv/bin/python -m py_compile $(find core handlers models utils -name '*.py' -print) server.py tests/test_stage12_workflows.py tests/test_stage34_workflows.py
# exit 0

git diff --check
# exit 0
```

No live Notion integration test was run.
