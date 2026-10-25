"""Focused offline coverage for the Stage 1/2 write workflows."""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.block_converter import MarkdownConverter
from handlers.write_handler import WriteHandler


def run(coro):
    return asyncio.run(coro)


def prop(name):
    return {"type": "title", "title": [{"plain_text": name}]}


class WorkflowClient:
    def __init__(self, rows=None, blocks=None):
        self.rows = {}
        for row_id, name in (rows or {}).items():
            self.rows[row_id] = {
                "id": row_id, "url": f"https://notion/{row_id}",
                "last_edited_time": "v1",
                "properties": {"Name": prop(name), "Status": {
                    "type": "select", "select": {"name": "Todo"}}},
            }
        self.blocks = list(blocks or [])
        self.next_id = 1
        self.updates = []
        self.deletes = []

    async def retrieve_database(self, database_id):
        return {"id": database_id, "properties": {
            "Name": {"type": "title", "title": {}},
            "Status": {"type": "select", "select": {"options": []}},
        }}

    async def query_database(self, database_id, filter_obj=None, page_size=100, start_cursor=None, **kwargs):
        matches = []
        wanted = None
        if filter_obj:
            spec = next(iter(filter_obj.values()))
            wanted = spec.get("equals") if isinstance(spec, dict) else None
            if isinstance(wanted, dict):
                wanted = wanted.get("name")
        for page in self.rows.values():
            value = page["properties"]["Name"]["title"][0]["plain_text"]
            if wanted is None or value == wanted:
                matches.append(page)
        return {"results": list(matches)[:page_size], "has_more": False, "next_cursor": None}

    async def retrieve_page(self, page_id):
        return self.rows.get(page_id, {"id": page_id, "properties": {}, "last_edited_time": "v1"})

    async def create_page(self, parent, properties, children=None):
        page_id = f"new{len(self.rows) + 1}"
        name = properties["Name"]["title"][0]["text"]["content"]
        status_value = properties.get("Status", {}).get("select", {}).get("name", "Todo")
        self.rows[page_id] = {"id": page_id, "url": f"https://notion/{page_id}",
                              "last_edited_time": "v1", "properties": {
                                  "Name": prop(name), "Status": {"type": "select", "select": {"name": status_value}}}}
        return {"id": page_id, "url": self.rows[page_id]["url"]}

    async def update_page_properties(self, page_id, properties):
        page = self.rows[page_id]
        for name, value in properties.items():
            ptype = next(iter(value))
            raw = value[ptype]
            if ptype == "title":
                raw = [{"plain_text": raw[0]["text"]["content"]}]
            elif ptype == "select":
                raw = {"name": raw["name"]}
            page["properties"][name] = {"type": ptype, ptype: raw}
        page["last_edited_time"] = f"v{len(self.updates) + 2}"
        self.updates.append(page_id)
        return {"id": page_id}

    async def retrieve_block_children(self, parent_id, page_size=100, start_cursor=None):
        return {"results": list(self.blocks), "has_more": False}

    async def append_block_children(self, parent_id, children, after=None):
        made = []
        for child in children:
            item = dict(child)
            item["id"] = f"b{self.next_id}"
            self.next_id += 1
            made.append(item)
        if after:
            idx = next((i for i, block in enumerate(self.blocks) if block.get("id") == after), len(self.blocks))
            self.blocks[idx + 1:idx + 1] = made
        else:
            self.blocks.extend(made)
        return {"results": made}

    async def update_block(self, block_id, data):
        for i, block in enumerate(self.blocks):
            if block.get("id") == block_id:
                self.blocks[i] = {"id": block_id, **data}
                return self.blocks[i]
        return {"error": True, "code": "NOT_FOUND", "message": "missing"}

    async def delete_block(self, block_id):
        self.blocks = [b for b in self.blocks if b.get("id") != block_id]
        self.deletes.append(block_id)
        return {"id": block_id}


class TestUpsertWorkflow(unittest.TestCase):
    def handler(self, client):
        return WriteHandler(client, MarkdownConverter(), None)

    def test_create_then_idempotency_replays_without_duplicate(self):
        client = WorkflowClient()
        handler = self.handler(client)
        first = run(handler.upsert_database_row("db", "Name", "A", {"Status": "Done"},
                                                idempotency_key="req-1"))
        self.assertFalse(first.get("error"), first)
        self.assertEqual(first["created"], 1)
        second = run(handler.upsert_database_row("db", "Name", "A", {"Status": "Done"},
                                                 idempotency_key="req-1"))
        self.assertEqual(second, first)
        self.assertEqual(len(client.rows), 1)

    def test_duplicate_match_is_conflict(self):
        client = WorkflowClient({"r1": "A", "r2": "A"})
        result = run(self.handler(client).upsert_database_row("db", "Name", "A", {"Status": "Done"}))
        self.assertEqual(result["code"], "CONFLICT")
        self.assertEqual(client.updates, [])

    def test_expected_fingerprint_rejects_stale_update(self):
        client = WorkflowClient({"r1": "A"})
        handler = self.handler(client)
        current = handler._page_fingerprint(client.rows["r1"])
        client.rows["r1"]["last_edited_time"] = "changed"
        result = run(handler.upsert_database_row("db", "Name", "A", {"Status": "Done"},
                                                 expected_fingerprint=current))
        self.assertEqual(result["code"], "CONFLICT")
        self.assertEqual(client.updates, [])


class TestReplaceAndBulkWorkflow(unittest.TestCase):
    def handler(self, client):
        return WriteHandler(client, MarkdownConverter(), None)

    def test_replace_updates_deletes_and_preserves_id(self):
        old_a = {"id": "a", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "A"}]}}
        old_b = {"id": "b", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "B"}]}}
        client = WorkflowClient(blocks=[old_a, old_b])
        desired = [
            {"id": "a", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "A2"}}]}},
        ]
        result = run(self.handler(client).replace_page_content("page", blocks=desired))
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(client.blocks[0]["id"], "a")
        self.assertEqual(client.blocks[0]["paragraph"]["rich_text"][0]["text"]["content"], "A2")

    def test_bulk_dry_run_has_no_updates_and_requires_filter(self):
        client = WorkflowClient({"r1": "A", "r2": "B"})
        handler = self.handler(client)
        invalid = run(handler.bulk_update_rows("db", {}, {"Status": "Done"}))
        self.assertEqual(invalid["code"], "INVALID_INPUT")
        result = run(handler.bulk_update_rows("db", {"property": "Name"},
                                              {"Status": "Done"}, dry_run=True))
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(client.updates, [])


if __name__ == "__main__":
    unittest.main()
