"""Query handler - list database rows with parsed property values."""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Hard cap on rows returned by a fetch_all sweep, to bound the response size.
MAX_ROWS_LIMIT = 2000


class QueryHandler:
    """Handle notion_query_database tool."""

    def __init__(self, client):
        self.client = client

    async def query_database(self, database_id: str, filter_obj: Optional[dict] = None,
                             sorts: Optional[list] = None, page_size: int = 50,
                             start_cursor: Optional[str] = None, fetch_all: bool = False,
                             max_rows: int = 500) -> dict:
        """Query a database and return rows with parsed property values.

        Default behavior is unchanged: one request, page_size clamped to 100,
        and has_more/next_cursor returned so callers can page through manually.

        Pagination extensions:
          - start_cursor: begin at a cursor returned by a previous call.
          - fetch_all: loop, following next_cursor until exhausted or until
            max_rows rows have been collected, then return the combined rows,
            has_more/next_cursor, total and pages_fetched.
        """
        if page_size < 1:
            page_size = 1
        if page_size > 100:
            page_size = 100

        if not fetch_all:
            try:
                result = await self._query_once(
                    database_id, filter_obj, sorts, page_size, start_cursor)
            except Exception as e:
                return {"error": True, "code": "QUERY_FAILED", "message": str(e)}
            if result.get("error"):
                return result
            rows = [self._parse_row(item) for item in result.get("results", [])]
            return {
                "database_id": database_id,
                "total": len(rows),
                "has_more": result.get("has_more", False),
                "next_cursor": result.get("next_cursor"),
                "rows": rows,
            }

        if max_rows < 1:
            max_rows = 1
        if max_rows > MAX_ROWS_LIMIT:
            max_rows = MAX_ROWS_LIMIT

        all_rows = []
        cursor = start_cursor
        pages_fetched = 0
        has_more = False
        next_cursor = None
        while True:
            try:
                result = await self._query_once(
                    database_id, filter_obj, sorts, page_size, cursor)
            except Exception as e:
                return {"error": True, "code": "QUERY_FAILED", "message": str(e)}
            if result.get("error"):
                return result
            pages_fetched += 1
            for item in result.get("results", []):
                all_rows.append(self._parse_row(item))
            has_more = result.get("has_more", False)
            next_cursor = result.get("next_cursor")
            if len(all_rows) >= max_rows:
                truncated = len(all_rows) > max_rows
                all_rows = all_rows[:max_rows]
                # More data exists beyond the cap (either dropped here or
                # signalled by Notion itself); keep the cursor so the caller
                # can continue.
                has_more = has_more or truncated
                if not has_more:
                    next_cursor = None
                break
            if not has_more or not next_cursor:
                has_more = False
                next_cursor = None
                break
            cursor = next_cursor

        return {
            "database_id": database_id,
            "total": len(all_rows),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "pages_fetched": pages_fetched,
            "rows": all_rows,
        }

    async def _query_once(self, database_id, filter_obj, sorts, page_size, start_cursor):
        """Single query call. Only sends start_cursor when set, so clients that
        predate pagination keep working for the default path."""
        if start_cursor:
            return await self.client.query_database(
                database_id, filter_obj=filter_obj, sorts=sorts,
                page_size=page_size, start_cursor=start_cursor)
        return await self.client.query_database(
            database_id, filter_obj=filter_obj, sorts=sorts, page_size=page_size)

    def _parse_row(self, item: dict) -> dict:
        properties = {}
        for name, prop in item.get("properties", {}).items():
            properties[name] = self._parse_property(prop)
        return {
            "id": item.get("id", ""),
            "url": item.get("url", ""),
            "created": item.get("created_time", ""),
            "last_edited": item.get("last_edited_time", ""),
            "properties": properties,
        }

    def _parse_property(self, prop: dict) -> Any:
        prop_type = prop.get("type", "")
        data = prop.get(prop_type, {})
        if prop_type == "title":
            return "".join(t.get("plain_text", "") for t in data)
        elif prop_type == "rich_text":
            return "".join(t.get("plain_text", "") for t in data)
        elif prop_type == "number":
            return data
        elif prop_type == "select":
            return data.get("name") if data else None
        elif prop_type == "multi_select":
            return [o.get("name") for o in data] if data else []
        elif prop_type == "date":
            return {"start": data.get("start"), "end": data.get("end")} if data else None
        elif prop_type == "checkbox":
            return bool(data)
        elif prop_type in ("url", "email", "phone_number"):
            return data
        elif prop_type == "status":
            return data.get("name") if data else None
        elif prop_type == "formula":
            return data.get("type")
        elif prop_type == "relation":
            return [r.get("id") for r in data] if data else []
        elif prop_type == "people":
            return [p.get("name") or p.get("id") for p in data] if data else []
        return str(data) if data else None
