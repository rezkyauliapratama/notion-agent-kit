"""Regression tests for the P1 fix package (production issues 2026-10-05).

Covers:
1. Block batching order: >100-block documents were scrambled by concurrent
   waves (104 blocks landed as 100 + 4 reversed). Sequential is now default.
2. number format alias: 'idr' -> 'rupiah' (Notion rejects ISO currency codes).
3. date range values: {"start","end"} double-wrapped -> 400 "start should be a
   string".
4. relation property support (previously ValueError "unsupported schema type").
5. read-only property types return a clear INVALID_INPUT.
6. notion_update_page_properties: type-aware conversion from the page schema.
7. notion_archive_page: page/database auto-detect + idempotency.
8. notion_query_database pagination: start_cursor / fetch_all / max_rows.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.block_batcher import BlockBatcher
from core.notion_client import NotionClient
from core.schema_cache import SchemaCache
from handlers.query_handler import QueryHandler
from handlers.write_handler import WriteHandler


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Block batching order
# ---------------------------------------------------------------------------

class RecordingClient:
    """Records the order in which chunks reach append_block_children."""

    def __init__(self):
        self.calls = []

    async def append_block_children(self, parent_block_id, children):
        self.calls.append(list(children))
        return {"results": children}


class TestBatchOrdering(unittest.TestCase):
    def _blocks(self, n):
        return [{"type": "paragraph", "id": i} for i in range(n)]

    def test_101_blocks_stay_in_order(self):
        client = RecordingClient()
        batcher = BlockBatcher(client=client)
        blocks = self._blocks(101)
        stats = run(batcher.append_blocks("parent", blocks))
        self.assertEqual(stats["total_blocks"], 101)
        self.assertEqual(stats["batches"], 2)
        self.assertEqual([len(c) for c in client.calls], [100, 1])
        sent = [b for chunk in client.calls for b in chunk]
        self.assertEqual([b["id"] for b in sent], list(range(101)))

    def test_250_blocks_stay_in_order(self):
        client = RecordingClient()
        batcher = BlockBatcher(client=client)
        blocks = self._blocks(250)
        stats = run(batcher.append_blocks("parent", blocks))
        self.assertEqual(stats["total_blocks"], 250)
        self.assertEqual(stats["batches"], 3)
        self.assertEqual([len(c) for c in client.calls], [100, 100, 50])
        sent = [b for chunk in client.calls for b in chunk]
        self.assertEqual([b["id"] for b in sent], list(range(250)))

    def test_sequential_is_default(self):
        client = RecordingClient()
        batcher = BlockBatcher(client=client)
        # Zero-arg call must work (backward-compatible signature) and be ordered.
        run(batcher.append_blocks("parent", self._blocks(101)))
        self.assertEqual([len(c) for c in client.calls], [100, 1])

    def test_empty_blocks(self):
        client = RecordingClient()
        stats = run(BlockBatcher(client=client).append_blocks("parent", []))
        self.assertEqual(stats["total_blocks"], 0)
        self.assertEqual(client.calls, [])

    def test_retry_still_applies_per_batch(self):
        class FlakyClient:
            def __init__(self):
                self.attempts = 0

            async def append_block_children(self, parent_block_id, children):
                self.attempts += 1
                if self.attempts < 2:
                    raise RuntimeError("transient")
                return {"results": children}

        client = FlakyClient()
        stats = run(BlockBatcher(client=client).append_blocks("parent", self._blocks(10)))
        self.assertEqual(stats["total_blocks"], 10)
        self.assertEqual(client.attempts, 2)


# ---------------------------------------------------------------------------
# 2. number format aliases
# ---------------------------------------------------------------------------

class TestNumberFormatAlias(unittest.TestCase):
    def test_simple_idr_maps_to_rupiah(self):
        props = WriteHandler._build_database_properties(
            {"Amount": {"type": "number", "format": "idr"}})
        self.assertEqual(props["Amount"], {"number": {"format": "rupiah"}})

    def test_simple_currency_aliases(self):
        cases = {"usd": "dollar", "eur": "euro", "gbp": "pound", "jpy": "yen",
                 "sgd": "singapore_dollar", "myr": "ringgit", "inr": "rupee"}
        for alias, expected in cases.items():
            props = WriteHandler._build_database_properties(
                {"X": {"type": "number", "format": alias}})
            self.assertEqual(props["X"]["number"]["format"], expected, alias)

    def test_raw_format_alias_is_normalized(self):
        props = WriteHandler._build_database_properties(
            {"Amount": {"number": {"format": "idr"}}})
        self.assertEqual(props["Amount"], {"number": {"format": "rupiah"}})

    def test_valid_notion_format_passes_through(self):
        props = WriteHandler._build_database_properties(
            {"Amount": {"type": "number", "format": "number_with_commas"}})
        self.assertEqual(props["Amount"], {"number": {"format": "number_with_commas"}})

    def test_default_format(self):
        props = WriteHandler._build_database_properties({"Amount": {"type": "number"}})
        self.assertEqual(props["Amount"], {"number": {"format": "number"}})

    def test_unknown_format_rejected_with_valid_list(self):
        with self.assertRaises(ValueError) as ctx:
            WriteHandler._build_database_properties(
                {"Amount": {"type": "number", "format": "bogus"}})
        msg = str(ctx.exception)
        self.assertIn("bogus", msg)
        self.assertIn("rupiah", msg)

    def test_unknown_format_surfaces_as_invalid_input(self):
        handler = WriteHandler(client=None, converter=None, batcher=None)
        result = run(handler.create_database(
            "page1", "DB", {"Amount": {"type": "number", "format": "bogus"}}))
        self.assertEqual(result["code"], "INVALID_INPUT")


# ---------------------------------------------------------------------------
# 3. date range values
# ---------------------------------------------------------------------------

class TestDateRange(unittest.TestCase):
    def test_range_dict_passes_through_unwrapped(self):
        schema = {"Due": {"type": "date"}}
        out = WriteHandler._convert_row_properties(
            schema, {"Due": {"start": "2026-10-01", "end": "2026-10-07"}})
        self.assertEqual(out["Due"], {"date": {"start": "2026-10-01", "end": "2026-10-07"}})

    def test_start_only_dict(self):
        out = WriteHandler._convert_row_properties(
            {"Due": {"type": "date"}}, {"Due": {"start": "2026-10-01", "end": None}})
        self.assertEqual(out["Due"], {"date": {"start": "2026-10-01"}})

    def test_time_zone_supported(self):
        out = WriteHandler._convert_row_properties(
            {"Due": {"type": "date"}},
            {"Due": {"start": "2026-10-01", "time_zone": "Asia/Jakarta"}})
        self.assertEqual(out["Due"]["date"]["time_zone"], "Asia/Jakarta")

    def test_string_behavior_preserved(self):
        out = WriteHandler._convert_row_properties(
            {"Due": {"type": "date"}}, {"Due": "2026-10-01"})
        self.assertEqual(out["Due"], {"date": {"start": "2026-10-01"}})

    def test_empty_dict_clears_date(self):
        out = WriteHandler._convert_row_properties(
            {"Due": {"type": "date"}}, {"Due": {}})
        self.assertEqual(out["Due"], {"date": {}})


# ---------------------------------------------------------------------------
# 4 & 5. relation + read-only types
# ---------------------------------------------------------------------------

class TestRelationAndReadOnly(unittest.TestCase):
    def test_relation_list_of_ids(self):
        out = WriteHandler._convert_row_properties(
            {"Rel": {"type": "relation"}}, {"Rel": ["a", "b"]})
        self.assertEqual(out["Rel"], {"relation": [{"id": "a"}, {"id": "b"}]})

    def test_relation_list_of_dicts(self):
        out = WriteHandler._convert_row_properties(
            {"Rel": {"type": "relation"}}, {"Rel": [{"id": "a"}, {"id": "b"}]})
        self.assertEqual(out["Rel"], {"relation": [{"id": "a"}, {"id": "b"}]})

    def test_relation_single_string(self):
        out = WriteHandler._convert_row_properties(
            {"Rel": {"type": "relation"}}, {"Rel": "a"})
        self.assertEqual(out["Rel"], {"relation": [{"id": "a"}]})

    def test_relation_empty_clears(self):
        for empty in ([], None, ""):
            out = WriteHandler._convert_row_properties(
                {"Rel": {"type": "relation"}}, {"Rel": empty})
            self.assertEqual(out["Rel"], {"relation": []})

    def test_relation_invalid_item(self):
        with self.assertRaises(ValueError):
            WriteHandler._convert_row_properties(
                {"Rel": {"type": "relation"}}, {"Rel": [123]})

    def test_readonly_types_raise_clear_error(self):
        for ptype in ("formula", "rollup", "created_time", "created_by",
                      "last_edited_time", "last_edited_by", "unique_id"):
            with self.assertRaises(ValueError) as ctx:
                WriteHandler._convert_row_properties(
                    {"P": {"type": ptype}}, {"P": "x"})
            self.assertIn("read-only", str(ctx.exception), ptype)

    def test_readonly_surfaces_as_invalid_input_via_handler(self):
        class FakeSchemaClient:
            async def retrieve_database(self, database_id):
                return {"id": database_id, "properties": {
                    "Name": {"type": "title", "title": {}},
                    "Total": {"type": "rollup", "rollup": {}},
                }}

        handler = WriteHandler(client=FakeSchemaClient(), converter=None,
                               batcher=None, cache=SchemaCache())
        result = run(handler.add_database_row("db1", {"Total": 5}))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertIn("read-only", result["message"])


# ---------------------------------------------------------------------------
# 6. update_page_properties
# ---------------------------------------------------------------------------

class UpdateClient:
    def __init__(self):
        self.updated = None

    async def retrieve_page(self, page_id):
        return {
            "id": page_id,
            "object": "page",
            "properties": {
                "Name": {"id": "t", "type": "title", "title": [{"plain_text": "old"}]},
                "Count": {"id": "n", "type": "number", "number": 0},
                "Status": {"id": "s", "type": "status", "status": {"name": "Todo"}},
                "Kind": {"id": "sel", "type": "select", "select": None},
                "Tags": {"id": "ms", "type": "multi_select", "multi_select": []},
                "Due": {"id": "d", "type": "date", "date": None},
                "Done": {"id": "c", "type": "checkbox", "checkbox": False},
                "Site": {"id": "u", "type": "url", "url": None},
                "Mail": {"id": "e", "type": "email", "email": None},
                "Phone": {"id": "p", "type": "phone_number", "phone_number": None},
                "Rel": {"id": "r", "type": "relation", "relation": []},
                "Note": {"id": "rt", "type": "rich_text", "rich_text": []},
            },
        }

    async def update_page_properties(self, page_id, properties):
        self.updated = properties
        return {"id": page_id, "object": "page", "properties": properties}


class TestUpdatePageProperties(unittest.TestCase):
    def setUp(self):
        self.client = UpdateClient()
        self.handler = WriteHandler(client=self.client, converter=None, batcher=None)

    def test_per_type_conversion(self):
        result = run(self.handler.update_page_properties("page1", {
            "Name": "Kopi",
            "Count": 5,
            "Status": "Done",
            "Kind": "Food",
            "Tags": ["a", "b"],
            "Due": {"start": "2026-10-01", "end": "2026-10-07"},
            "Done": True,
            "Site": "https://notion.so",
            "Mail": "a@b.com",
            "Phone": "123",
            "Rel": ["x", "y"],
            "Note": "hello",
        }))
        self.assertNotIn("error", result)
        self.assertEqual(result["page_id"], "page1")
        self.assertEqual(sorted(result["updated"]), sorted([
            "Name", "Count", "Status", "Kind", "Tags", "Due", "Done",
            "Site", "Mail", "Phone", "Rel", "Note"]))
        p = self.client.updated
        self.assertEqual(p["Name"]["title"][0]["text"]["content"], "Kopi")
        self.assertEqual(p["Count"], {"number": 5})
        self.assertEqual(p["Status"], {"status": {"name": "Done"}})
        self.assertEqual(p["Kind"], {"select": {"name": "Food"}})
        self.assertEqual(p["Tags"], {"multi_select": [{"name": "a"}, {"name": "b"}]})
        self.assertEqual(p["Due"], {"date": {"start": "2026-10-01", "end": "2026-10-07"}})
        self.assertEqual(p["Done"], {"checkbox": True})
        self.assertEqual(p["Site"], {"url": "https://notion.so"})
        self.assertEqual(p["Mail"], {"email": "a@b.com"})
        self.assertEqual(p["Phone"], {"phone_number": "123"})
        self.assertEqual(p["Rel"], {"relation": [{"id": "x"}, {"id": "y"}]})
        self.assertEqual(p["Note"]["rich_text"][0]["text"]["content"], "hello")

    def test_raw_api_object_passthrough(self):
        raw = {"rich_text": [{"type": "text", "text": {"content": "x"}}]}
        run(self.handler.update_page_properties("page1", {"Note": raw}))
        self.assertEqual(self.client.updated["Note"], raw)

    def test_unknown_property_returns_invalid_input(self):
        result = run(self.handler.update_page_properties("page1", {"Nope": 1}))
        self.assertEqual(result["code"], "INVALID_INPUT")
        self.assertIn("Nope", result["message"])

    def test_readonly_property_returns_invalid_input(self):
        class ROClient(UpdateClient):
            async def retrieve_page(self, page_id):
                return {"id": page_id, "properties": {
                    "Total": {"type": "formula", "formula": {}}}}

        handler = WriteHandler(client=ROClient(), converter=None, batcher=None)
        result = run(handler.update_page_properties("p", {"Total": 1}))
        self.assertEqual(result["code"], "INVALID_INPUT")

    def test_empty_properties_rejected(self):
        result = run(self.handler.update_page_properties("page1", {}))
        self.assertEqual(result["code"], "INVALID_INPUT")


# ---------------------------------------------------------------------------
# 7. archive page / database
# ---------------------------------------------------------------------------

class TestArchiveClientEndpoints(unittest.TestCase):
    def _client_with_routes(self, routes):
        client = NotionClient(token="test-token")
        calls = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            handler = routes.get(path.split("/")[0])
            if handler is None:
                return {"error": True, "code": "NOT_FOUND", "message": "no route"}
            return handler(path, kwargs)

        client._request = fake_request
        return client, calls

    def test_page_archive(self):
        client, calls = self._client_with_routes({
            "pages": lambda path, kw: {"object": "page", "id": "p1", "archived": True},
        })
        result = run(client.archive_page("p1", True, "auto"))
        self.assertEqual(result["object"], "page")
        self.assertEqual(calls[0][1], "pages/p1")
        self.assertEqual(calls[0][2]["json"], {"archived": True})

    def test_database_fallback_on_page_404(self):
        def pages_route(path, kw):
            return {"error": True, "code": "NOT_FOUND", "message": "not a page"}

        client, calls = self._client_with_routes({
            "pages": pages_route,
            "databases": lambda path, kw: {"object": "database", "id": "db1", "archived": True},
        })
        result = run(client.archive_page("db1", True, "auto"))
        self.assertEqual(result["object"], "database")
        self.assertEqual([c[1] for c in calls], ["pages/db1", "databases/db1"])

    def test_forced_page_does_not_fall_back(self):
        def pages_route(path, kw):
            return {"error": True, "code": "NOT_FOUND", "message": "nope"}

        client, calls = self._client_with_routes({"pages": pages_route})
        result = run(client.archive_page("x", True, "page"))
        self.assertTrue(result.get("error"))
        self.assertEqual(len(calls), 1)


class ArchiveHandlerClient:
    def __init__(self, kind="page"):
        self.kind = kind
        self.calls = []

    async def archive_page(self, object_id, archived, object_type="auto"):
        self.calls.append((object_id, archived, object_type))
        return {"object": self.kind, "id": object_id, "archived": archived}


class TestArchiveHandler(unittest.TestCase):
    def test_archive_page_output(self):
        client = ArchiveHandlerClient("page")
        handler = WriteHandler(client=client, converter=None, batcher=None)
        result = run(handler.archive_page("p1"))
        self.assertEqual(result["object"], "page")
        self.assertEqual(result["id"], "p1")
        self.assertTrue(result["archived"])

    def test_restore_flag(self):
        client = ArchiveHandlerClient("page")
        handler = WriteHandler(client=client, converter=None, batcher=None)
        result = run(handler.archive_page("p1", restore=True))
        self.assertFalse(result["archived"])
        self.assertEqual(client.calls[0][1], False)

    def test_database_auto_detected(self):
        client = ArchiveHandlerClient("database")
        handler = WriteHandler(client=client, converter=None, batcher=None)
        result = run(handler.archive_page("db1"))
        self.assertEqual(result["object"], "database")

    def test_idempotent_second_archive(self):
        client = ArchiveHandlerClient("page")
        handler = WriteHandler(client=client, converter=None, batcher=None)
        first = run(handler.archive_page("p1"))
        second = run(handler.archive_page("p1"))
        self.assertTrue(first["archived"])
        self.assertTrue(second["archived"])
        self.assertNotIn("error", second)


# ---------------------------------------------------------------------------
# 8. query pagination
# ---------------------------------------------------------------------------

class PagedQueryClient:
    """Fake client serving 5 rows across 3 pages via shared cursors."""

    def __init__(self):
        self.calls = []
        self.pages = {
            None: (["r1", "r2"], True, "c1"),
            "c1": (["r3", "r4"], True, "c2"),
            "c2": (["r5"], False, None),
        }

    def _row(self, rid):
        return {
            "id": rid, "url": f"https://notion.so/{rid}",
            "created_time": "2026-10-01T00:00:00.000Z",
            "last_edited_time": "2026-10-02T00:00:00.000Z",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": rid}]}},
        }

    async def query_database(self, database_id, filter_obj=None, sorts=None,
                             page_size=100, start_cursor=None):
        self.calls.append(start_cursor)
        ids, has_more, next_cursor = self.pages[start_cursor]
        return {
            "results": [self._row(i) for i in ids],
            "has_more": has_more,
            "next_cursor": next_cursor,
        }


class TestQueryPagination(unittest.TestCase):
    def test_default_unchanged(self):
        client = PagedQueryClient()
        handler = QueryHandler(client=client)
        result = run(handler.query_database("db1", page_size=50))
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], "c1")
        self.assertEqual(client.calls, [None])

    def test_start_cursor(self):
        client = PagedQueryClient()
        handler = QueryHandler(client=client)
        result = run(handler.query_database("db1", start_cursor="c1"))
        self.assertEqual([r["id"] for r in result["rows"]], ["r3", "r4"])
        self.assertEqual(client.calls, ["c1"])

    def test_fetch_all_collects_every_page(self):
        client = PagedQueryClient()
        handler = QueryHandler(client=client)
        result = run(handler.query_database("db1", fetch_all=True, max_rows=500))
        self.assertEqual(result["total"], 5)
        self.assertEqual([r["id"] for r in result["rows"]],
                         ["r1", "r2", "r3", "r4", "r5"])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["pages_fetched"], 3)
        self.assertEqual(client.calls, [None, "c1", "c2"])

    def test_fetch_all_respects_max_rows(self):
        client = PagedQueryClient()
        handler = QueryHandler(client=client)
        result = run(handler.query_database("db1", fetch_all=True, max_rows=2))
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], "c1")
        self.assertEqual(result["pages_fetched"], 1)

    def test_fetch_all_from_start_cursor(self):
        client = PagedQueryClient()
        handler = QueryHandler(client=client)
        result = run(handler.query_database("db1", fetch_all=True,
                                            start_cursor="c1", max_rows=500))
        self.assertEqual([r["id"] for r in result["rows"]], ["r3", "r4", "r5"])
        self.assertFalse(result["has_more"])

    def test_fetch_all_clamps_max_rows(self):
        client = PagedQueryClient()
        handler = QueryHandler(client=client)
        result = run(handler.query_database("db1", fetch_all=True, max_rows=99999))
        # Clamped to MAX_ROWS_LIMIT (2000); all 5 rows still returned.
        self.assertEqual(result["total"], 5)


if __name__ == "__main__":
    unittest.main()
