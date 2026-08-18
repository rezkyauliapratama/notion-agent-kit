"""Query handler - list database rows with parsed property values."""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class QueryHandler:
    """Handle notion_query_database tool."""

    def __init__(self, client):
        self.client = client

    async def query_database(self, database_id: str, filter_obj: Optional[dict] = None,
                             sorts: Optional[list] = None, page_size: int = 50,
                             start_cursor: Optional[str] = None) -> dict:
        """Query a database and return rows with parsed property values.

        Paginates automatically: if the database has more rows than page_size,
        returns has_more=true and next_cursor so callers can page through.
        """
        if page_size < 1:
            page_size = 1
        if page_size > 100:
            page_size = 100
        try:
            result = await self.client.query_database(
                database_id, filter_obj=filter_obj, sorts=sorts,
                page_size=page_size,
            )
        except Exception as e:
            return {"error": True, "code": "QUERY_FAILED", "message": str(e)}
        if result.get("error"):
            return result

        rows = []
        for item in result.get("results", []):
            properties = {}
            for name, prop in item.get("properties", {}).items():
                properties[name] = self._parse_property(prop)
            rows.append({
                "id": item.get("id", ""),
                "url": item.get("url", ""),
                "created": item.get("created_time", ""),
                "last_edited": item.get("last_edited_time", ""),
                "properties": properties,
            })

        return {
            "database_id": database_id,
            "total": len(rows),
            "has_more": result.get("has_more", False),
            "next_cursor": result.get("next_cursor"),
            "rows": rows,
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
