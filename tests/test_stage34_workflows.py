"""Offline Stage 3/4 contract tests using provider-neutral fake clients."""

import asyncio
import copy
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.state_store import SQLiteStateStore
from handlers.write_handler import WriteHandler


def run(coro):
    return asyncio.run(coro)


def paragraph(block_id, text):
    return {"id": block_id, "type": "paragraph",
            "paragraph": {"rich_text": [{"plain_text": text}]}}


class NestedClient:
    supports_ordered_after = False

    def __init__(self):
        self.children = {"page": [
            {"id": "toggle", "type": "toggle", "has_children": True,
             "toggle": {"rich_text": [{"plain_text": "Parent"}]}},
            paragraph("tail", "Tail"),
        ], "toggle": [paragraph("child", "old")]}
        self.calls = []
        self.counter = 0

    async def retrieve_page(self, page_id):
        return {"id": page_id, "properties": {}, "last_edited_time": "v1"}

    async def retrieve_block_children(self, parent_id, page_size=100, start_cursor=None):
        return {"results": copy.deepcopy(self.children.get(parent_id, [])), "has_more": False}

    def _find(self, block_id, nodes=None):
        nodes = self.children.get("page", []) if nodes is None else nodes
        for block in nodes:
            if block.get("id") == block_id:
                return block
            found = self._find(block_id, self.children.get(block.get("id"), [])) if block.get("has_children") else None
            if found:
                return found
        return None

    async def update_block(self, block_id, data):
        self.calls.append(("update", block_id))
        current = self._find(block_id)
        if not current:
            return {"error": True, "code": "NOT_FOUND"}
        current.update(copy.deepcopy(data))
        return copy.deepcopy(current)

    async def append_block_children(self, parent_id, children, after=None):
        self.calls.append(("append", parent_id, after))
        made = []
        target = self.children.setdefault(parent_id, [])
        for child in children:
            item = copy.deepcopy(child)
            self.counter += 1
            item["id"] = f"new{self.counter}"
            made.append(item)
        if after is None:
            target.extend(made)
        else:
            index = next(i for i, block in enumerate(target) if block["id"] == after)
            target[index + 1:index + 1] = made
        return {"results": copy.deepcopy(made)}

    async def delete_block(self, block_id):
        self.calls.append(("delete", block_id))
        for parent, nodes in self.children.items():
            self.children[parent] = [node for node in nodes if node.get("id") != block_id]
        self.children.pop(block_id, None)
        return {"id": block_id}


class PagedBulkClient:
    def __init__(self):
        self.rows = {}
        for i in range(3):
            self.rows[f"r{i}"] = {"id": f"r{i}", "last_edited_time": "v1",
                                   "properties": {"Name": {"type": "title",
                                                               "title": [{"plain_text": str(i)}]},
                                                  "Status": {"type": "select",
                                                              "select": {"name": "Todo"}}}}
        self.updates = []

    async def retrieve_database(self, database_id):
        return {"id": database_id, "properties": {"Name": {"type": "title"},
                "Status": {"type": "select"}}}

    async def query_database(self, database_id, filter_obj=None, page_size=100, start_cursor=None):
        ids = ["r0", "r1", "r2"]
        start = int(start_cursor or 0)
        end = min(start + page_size, len(ids))
        return {"results": [self.rows[x] for x in ids[start:end]],
                "has_more": end < len(ids), "next_cursor": str(end) if end < len(ids) else None}

    async def retrieve_page(self, page_id):
        return copy.deepcopy(self.rows[page_id])

    async def update_page_properties(self, page_id, properties):
        self.updates.append(page_id)
        for name, value in properties.items():
            kind = next(iter(value))
            raw = value[kind]
            if kind == "select":
                raw = {"name": raw["name"]}
            self.rows[page_id]["properties"][name] = {"type": kind, kind: raw}
        self.rows[page_id]["last_edited_time"] = "v2"
        return {"id": page_id}


class Stage34Tests(unittest.TestCase):
    def test_sqlite_idempotency_is_shared_expiring_and_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.sqlite3")
            first = SQLiteStateStore(path, idempotency_ttl=1)
            self.assertEqual(first.idempotency_store("op", "k", "hash", {"ok": 1})[0], "stored")
            second = SQLiteStateStore(path)
            self.assertEqual(second.idempotency_lookup("op", "k", "hash"), ("replay", {"ok": 1}))
            self.assertEqual(second.idempotency_lookup("op", "k", "other")[0], "conflict")
            time.sleep(1.05)
            self.assertIsNone(second.idempotency_lookup("op", "k", "hash"))
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)

    def test_audit_is_redacted_and_bounded(self):
        store = SQLiteStateStore(":memory:", audit_enabled=True, audit_retention=2)
        for value in ("a", "b", "c"):
            store.record_audit("write", {"token": "ntn_secret", "value": value})
        rows = store.list_audit(10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["result"]["token"], "[REDACTED]")

    def test_recursive_replace_updates_nested_child_identity(self):
        client = NestedClient()
        handler = WriteHandler(client, converter=None, batcher=None)
        desired = [{"id": "toggle", "type": "toggle", "toggle": {
            "rich_text": [{"type": "text", "text": {"content": "Parent"}}],
            "children": [{"id": "child", "type": "paragraph",
                          "paragraph": {"rich_text": [{"type": "text",
                          "text": {"content": "new"}}]}}] }}, paragraph("tail", "Tail")]
        result = run(handler.replace_page_content("page", blocks=desired))
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(client.children["toggle"][0]["id"], "child")
        self.assertEqual(client.children["toggle"][0]["paragraph"]["rich_text"][0]["text"]["content"], "new")

    def test_unproven_insertion_has_no_mutation(self):
        client = NestedClient()
        handler = WriteHandler(client, converter=None, batcher=None)
        desired = [
            {"id": "toggle", "type": "toggle", "toggle": {"rich_text": [{"plain_text": "Parent"}]}},
            paragraph("new", "New"), paragraph("tail", "Tail")]
        result = run(handler.replace_page_content("page", blocks=desired))
        self.assertEqual(result["code"], "ORDERING_UNPROVEN")
        self.assertEqual(client.calls, [])

    def test_resumable_bulk_paginates_sequentially_and_replays_safely(self):
        client = PagedBulkClient()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(os.path.join(directory, "state.sqlite3"))
            handler = WriteHandler(client, converter=None, batcher=None, state_store=store)
            result = run(handler.bulk_update_rows("db", {"property": "Name"},
                                                  {"Status": "Done"}, max_rows=600,
                                                  batch_size=2))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["matched"], 3)
            self.assertEqual(client.updates, ["r0", "r1", "r2"])
            again = run(handler.resume_bulk_job(result["job_id"]))
            self.assertEqual(again["status"], "completed")
            self.assertEqual(client.updates, ["r0", "r1", "r2"])

    def test_archive_metadata_is_additive(self):
        class Client:
            async def archive_page(self, page_id, archived, object_type):
                return {"object": "page", "id": page_id}
        result = run(WriteHandler(Client(), None, None).archive_page("p"))
        self.assertEqual(result["object"], "page")
        self.assertEqual(result["archived"], True)
        self.assertEqual(result["operation"], "notion_archive_page")
        self.assertTrue(result["changed"])

    def test_legacy_write_metadata_is_additive(self):
        class Client:
            async def retrieve_page(self, page_id):
                return {"id": page_id, "properties": {"Name": {"type": "title", "title": []}}}
            async def update_page_properties(self, page_id, properties):
                return {"id": page_id}
        result = run(WriteHandler(Client(), None, None).update_page_properties("p", {"Name": "x"}))
        self.assertEqual(result["updated"], ["Name"])
        self.assertEqual(result["operation"], "notion_update_page_properties")
        self.assertTrue(result["changed"])


if __name__ == "__main__":
    unittest.main()
