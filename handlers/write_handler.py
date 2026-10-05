"""Write handler - create pages, write documents, append blocks."""

import logging
import time
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

VALID_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do",
    "toggle", "quote", "callout", "code", "divider",
    "table", "table_row", "image", "bookmark", "embed",
    "link_preview", "column", "column_list", "table_of_contents",
    "synced_block", "template", "link_to_page", "equation",
    "file", "pdf", "video", "audio", "breadcrumb",
}

# Notion number-format identifiers. Callers routinely pass currency ISO codes
# (e.g. "idr") which Notion rejects with a 400 validation_error; the API only
# accepts its own names (e.g. "rupiah"). Map the common aliases explicitly.
NUMBER_FORMAT_ALIASES = {
    "idr": "rupiah",
    "usd": "dollar",
    "eur": "euro",
    "gbp": "pound",
    "jpy": "yen",
    "sgd": "singapore_dollar",
    "myr": "ringgit",
    "aud": "australian_dollar",
    "cad": "canadian_dollar",
    "chf": "franc",
    "cny": "yuan",
    "krw": "won",
    "inr": "rupee",
    "thb": "baht",
}

# Valid values for Notion number formats (aliases are resolved into these).
VALID_NUMBER_FORMATS = {
    "number", "number_with_commas", "percent", "dollar", "canadian_dollar",
    "euro", "pound", "yen", "ruble", "rupee", "won", "yuan", "real", "lira",
    "rupiah", "franc", "hong_kong_dollar", "new_zealand_dollar", "krona",
    "norwegian_krone", "mexican_peso", "rand", "new_taiwan_dollar",
    "danish_krone", "zloty", "baht", "forint", "koruna", "leu", "shekel",
    "chilean_peso", "philippine_peso", "dirham", "colombian_peso", "riyal",
    "ringgit", "lempira", "argentine_peso", "uruguayan_peso",
    "singapore_dollar", "australian_dollar",
}

# Property types Notion computes and rejects on write.
READONLY_PROPERTY_TYPES = {
    "rollup", "formula", "created_time", "created_by",
    "last_edited_time", "last_edited_by", "unique_id",
}

# Keys that mark a value as already being a Notion API property object.
API_PROPERTY_KEYS = (
    "title", "rich_text", "number", "select", "multi_select", "status",
    "date", "checkbox", "url", "email", "phone_number", "relation",
)

# Property types that can be copied when duplicating a page. Read-only types
# (formula, rollup, created_time, ...) and types not listed here are skipped.
COPYABLE_PROPERTY_TYPES = {
    "title", "rich_text", "number", "select", "status", "multi_select",
    "date", "checkbox", "url", "email", "phone_number", "relation",
}

# Container block types whose children are shipped inline by their parent and
# therefore must not be recursed into when duplicating a page.
_INLINE_CHILD_TYPES = {"table"}


def _plain_text(rich_text) -> str:
    """Extract plain text from a rich_text array.

    Handles both outgoing payloads (``text.content``) and Notion read-back
    responses (``plain_text``), which lets the same code sign outgoing blocks
    and verify read-back ones.
    """
    out = []
    for part in rich_text or []:
        if not isinstance(part, dict):
            continue
        if part.get("plain_text") is not None:
            out.append(part["plain_text"])
            continue
        text = part.get("text")
        if isinstance(text, dict):
            out.append(text.get("content", ""))
    return "".join(out)


def block_signature(block: dict):
    """Return a ``(type, text-snippet)`` signature for a block.

    Production rule 4: append responses can contain MORE results than blocks
    sent, so results are never validated by count. Verification reads the page
    back and compares these signatures instead.
    """
    block_type = (block.get("type") or "") if isinstance(block, dict) else ""
    data = block.get(block_type) or {} if isinstance(block, dict) else {}
    if block_type == "table_row":
        cells = data.get("cells") or []
        return (block_type, " | ".join(_plain_text(cell) for cell in cells)[:70])
    if block_type == "table":
        return (block_type, f"w{data.get('table_width')}")
    if block_type == "divider":
        return (block_type, "")
    rich_text = data.get("rich_text") if isinstance(data, dict) else None
    if rich_text is not None:
        return (block_type, _plain_text(rich_text)[:70])
    return (block_type, "")


def find_table_missing_children(blocks, path=()):
    """Return ``(path, block)`` for the first table block without inline rows.

    Production rule 3: Notion rejects a table sent without its rows with
    ``body.children[N].table.children should be defined``. Tables must ship
    with their ``table_row`` children inline. Recurses into containers so raw
    JSON nested tables are validated too.
    """
    for i, block in enumerate(blocks or []):
        if not isinstance(block, dict):
            continue
        here = path + (i,)
        block_type = block.get("type", "")
        if block_type == "table":
            children = (block.get("table") or {}).get("children")
            if not children or not isinstance(children, list):
                return here, block
        data = block.get(block_type)
        if isinstance(data, dict) and isinstance(data.get("children"), list):
            found = find_table_missing_children(data["children"], here)
            if found:
                return found
    return None


def _flatten_tree(nodes):
    out = []
    for node in nodes or []:
        out.append(node)
        out.extend(_flatten_tree(node.get("children", [])))
    return out


class WriteHandler:
    """Handle notion_write_document and notion_write_blocks tools."""

    def __init__(self, client, converter, batcher, cache=None):
        self.client = client
        self.converter = converter
        self.batcher = batcher
        self.cache = cache

    async def create_database(self, parent_page_id: str, title: str, properties: dict) -> dict:
        """Create a database with the given schema.

        properties accepts two formats:
        1. Simple: {"Amount": {"type": "number", "format": "idr"},
                     "Category": {"type": "select", "options": ["A", "B"]}}
        2. Raw Notion API format: {"Amount": {"number": {"format": "idr"}}}
        """
        if not title or not title.strip():
            return {"error": True, "code": "INVALID_INPUT", "message": "title is required"}
        if not properties or not isinstance(properties, dict):
            return {"error": True, "code": "INVALID_INPUT", "message": "properties must be a non-empty dict"}
        try:
            api_props = self._build_database_properties(properties)
        except ValueError as e:
            return {"error": True, "code": "INVALID_INPUT", "message": str(e)}
        # Notion requires exactly one title property in every database.
        # Auto-add a default one so callers that forget it don't get a 400.
        if not any(isinstance(v, dict) and "title" in v for v in api_props.values()):
            api_props["Name"] = {"title": {}}
        parent = {"type": "page_id", "page_id": parent_page_id}
        try:
            result = await self.client.create_database(parent, title, api_props)
        except Exception as e:
            return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        db_id = result.get("id", "")
        if self.cache:
            self.cache.set(db_id, result)
        return {
            "database_id": db_id,
            "url": result.get("url", ""),
            "title": title,
            "properties": list(api_props.keys()),
        }

    async def add_database_row(self, database_id: str, properties: dict) -> dict:
        """Add a row (page) to an existing database.

        Values are auto-converted to Notion API format based on the
        database's property schema (fetched + cached). Supports:
        title, rich_text, number, select, status, multi_select, date
        (including ranges as {"start": ..., "end": ...}), checkbox, url,
        email, phone_number, relation (ID string or list of IDs).
        Read-only types (formula, rollup, created_time, ...) return
        INVALID_INPUT.
        """
        if not properties or not isinstance(properties, dict):
            return {"error": True, "code": "INVALID_INPUT", "message": "properties must be a non-empty dict"}
        schema = await self._get_database_schema(database_id)
        if isinstance(schema, dict) and schema.get("error"):
            return schema
        try:
            api_props = self._convert_row_properties(schema, properties)
        except ValueError as e:
            return {"error": True, "code": "INVALID_INPUT", "message": str(e)}
        parent = {"type": "database_id", "database_id": database_id}
        try:
            result = await self.client.create_page(parent, api_props)
        except Exception as e:
            return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        return {
            "page_id": result.get("id", ""),
            "url": result.get("url", ""),
            "properties": list(api_props.keys()),
        }

    # --- Database schema helpers ---

    async def _get_database_schema(self, database_id: str) -> dict:
        if self.cache:
            cached = self.cache.get(database_id)
            if cached:
                return self._normalize_schema(cached.get("properties", {}))
        try:
            result = await self.client.retrieve_database(database_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        if self.cache:
            self.cache.set(database_id, result)
        return self._normalize_schema(result.get("properties", {}))

    @staticmethod
    def _normalize_schema(properties) -> dict:
        """Normalize a database schema to {name: {type: ...}}.

        Fixes a real production bug: the schema cache is shared between
        notion_inspect_database (which stores properties as a LIST of
        {name, type, options}) and notion_add_database_row (which expects a
        DICT keyed by property name). The cached list crashed
        add_database_row with "'list' object has no attribute 'get'".
        """
        if isinstance(properties, dict):
            return properties
        if isinstance(properties, list):
            normalized = {}
            for entry in properties:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                name = entry["name"]
                spec = {"type": entry.get("type", "rich_text")}
                options = entry.get("options")
                if options is not None:
                    spec["options"] = [
                        o.get("name") if isinstance(o, dict) else o for o in options
                    ]
                normalized[name] = spec
            return normalized
        return {}

    @staticmethod
    def _resolve_number_format(fmt: Optional[str]) -> str:
        """Resolve a number format alias (e.g. 'idr') to a Notion-valid name.

        Notion rejects unknown formats such as 'idr' with a 400
        validation_error; the valid value is 'rupiah'. Unknown formats are
        rejected explicitly rather than silently replaced.
        """
        if fmt is None:
            return "number"
        key = str(fmt).strip().lower()
        if key in NUMBER_FORMAT_ALIASES:
            return NUMBER_FORMAT_ALIASES[key]
        if key in VALID_NUMBER_FORMATS:
            return key
        raise ValueError(
            f"Unsupported number format '{fmt}'. Valid values: "
            f"{sorted(VALID_NUMBER_FORMATS)}. Common aliases are also accepted: "
            f"{sorted(NUMBER_FORMAT_ALIASES)}."
        )

    @staticmethod
    def _build_database_properties(properties: dict) -> dict:
        """Convert simple schema format to Notion API properties format."""
        SIMPLE_TO_API = {
            "title": "title",
            "rich_text": "rich_text",
            "number": "number",
            "select": "select",
            "multi_select": "multi_select",
            "status": "status",
            "date": "date",
            "checkbox": "checkbox",
            "url": "url",
            "email": "email",
            "phone_number": "phone_number",
        }
        api_props = {}
        for name, spec in properties.items():
            if not isinstance(spec, dict):
                raise ValueError(f"Property '{name}' must be a dict spec, got {type(spec).__name__}")
            # Raw Notion format already has the type as key, e.g. {"number": {...}}
            if any(k in spec for k in ("title", "rich_text", "number", "select",
                                       "multi_select", "status", "date", "checkbox",
                                       "url", "email", "phone_number")):
                raw = spec
                if isinstance(raw.get("number"), dict):
                    raw = dict(raw)
                    number_cfg = dict(raw["number"])
                    number_cfg["format"] = WriteHandler._resolve_number_format(
                        number_cfg.get("format"))
                    raw["number"] = number_cfg
                api_props[name] = raw
                continue
            ptype = spec.get("type", "rich_text")
            if ptype not in SIMPLE_TO_API:
                raise ValueError(f"Property '{name}': unsupported type '{ptype}'. "
                                 f"Supported: {sorted(SIMPLE_TO_API)}")
            api_key = SIMPLE_TO_API[ptype]
            if ptype == "number":
                fmt = WriteHandler._resolve_number_format(spec.get("format"))
                api_props[name] = {"number": {"format": fmt}}
            elif ptype in ("select", "multi_select", "status"):
                options = spec.get("options", [])
                option_objs = [{"name": o} if isinstance(o, str) else o for o in options]
                api_props[name] = {api_key: {"options": option_objs} if option_objs else {}}
            else:
                api_props[name] = {api_key: {}}
        return api_props

    @staticmethod
    def _normalize_date_value(value):
        """Normalize a date value into a Notion `date` payload.

        Accepts a plain ISO string ({"start": value}) or an already-formed dict
        ({"start", "end", "time_zone"}). Dict values used to be wrapped a second
        time as {"start": {...}} which Notion rejected with 400
        "start should be a string". Empty/None keys are dropped; a string keeps
        the previous behavior.
        """
        if isinstance(value, dict):
            result = {}
            for key in ("start", "end", "time_zone"):
                item = value.get(key)
                if item is not None and item != "":
                    result[key] = item
            return result
        return {"start": value}

    @staticmethod
    def _normalize_relation(value) -> list:
        """Convert relation input to Notion's [{"id": ...}] list.

        Accepts a single ID string, a list of ID strings, a list of
        {"id": ...} dicts, or a single {"id": ...} dict. Empty input clears
        the relation.
        """
        if value is None or value == "":
            return []
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, dict):
            items = [value]
        elif isinstance(value, list):
            items = value
        else:
            raise ValueError(f"Relation value must be an ID string, a list of IDs, "
                             f"or {{\"id\": ...}} objects. Got {type(value).__name__}")
        result = []
        for item in items:
            if isinstance(item, str):
                result.append({"id": item})
            elif isinstance(item, dict) and item.get("id"):
                result.append({"id": item["id"]})
            else:
                raise ValueError(f"Invalid relation item {item!r}. Expected an ID "
                                 f"string or {{\"id\": ...}}.")
        return result

    @staticmethod
    def _convert_property_value(name: str, ptype: Optional[str], value,
                                fallback_name_title: bool = False) -> dict:
        """Convert one plain value to a Notion API property payload.

        Shared by add_database_row and update_page_properties so both paths
        apply identical conversion rules. Values that already look like a
        Notion API object are passed through untouched.
        """
        if isinstance(value, dict) and any(k in value for k in API_PROPERTY_KEYS):
            return value
        if ptype == "title" or (ptype is None and fallback_name_title and isinstance(value, str)):
            return {"title": [{"type": "text", "text": {"content": str(value)}}]}
        elif ptype == "rich_text":
            return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}
        elif ptype == "number":
            return {"number": value}
        elif ptype == "select":
            return {"select": {"name": value}}
        elif ptype == "status":
            return {"status": {"name": value}}
        elif ptype == "multi_select":
            items = value if isinstance(value, list) else [value]
            return {"multi_select": [i if isinstance(i, dict) else {"name": i} for i in items]}
        elif ptype == "date":
            return {"date": WriteHandler._normalize_date_value(value)}
        elif ptype == "checkbox":
            return {"checkbox": bool(value)}
        elif ptype == "url":
            return {"url": value}
        elif ptype == "email":
            return {"email": value}
        elif ptype == "phone_number":
            return {"phone_number": value}
        elif ptype == "relation":
            return {"relation": WriteHandler._normalize_relation(value)}
        elif ptype in READONLY_PROPERTY_TYPES:
            raise ValueError(
                f"Property '{name}': type '{ptype}' is read-only and cannot be written. "
                f"Read-only types: {sorted(READONLY_PROPERTY_TYPES)}."
            )
        elif ptype is None:
            # Schema unknown: fall back to python type heuristics
            if isinstance(value, bool):
                return {"checkbox": value}
            elif isinstance(value, (int, float)):
                return {"number": value}
            elif isinstance(value, list):
                return {"multi_select": [i if isinstance(i, dict) else {"name": i} for i in value]}
            else:
                return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}
        else:
            raise ValueError(f"Property '{name}': unsupported schema type '{ptype}'")

    @staticmethod
    def _convert_row_properties(schema: dict, values: dict) -> dict:
        """Convert plain values to Notion API format based on schema types."""
        if not isinstance(schema, dict):
            schema = WriteHandler._normalize_schema(schema)
        converted = {}
        for name, value in values.items():
            prop_spec = schema.get(name, {})
            ptype = prop_spec.get("type") if isinstance(prop_spec, dict) else None
            fallback_name_title = name.lower() in ("title", "name", "description")
            converted[name] = WriteHandler._convert_property_value(
                name, ptype, value, fallback_name_title=fallback_name_title)
        return converted

    # --- Page property updates / archive ---

    async def update_page_properties(self, page_id: str, properties: dict) -> dict:
        """Update typed properties on an existing page.

        The page is fetched first to learn each property's name and type (a
        page's schema is not cacheable like a database's). Values are then
        converted with the same shared logic as add_database_row. Unknown
        property names return INVALID_INPUT listing the ones that exist.
        """
        start_time = time.monotonic()
        if not properties or not isinstance(properties, dict):
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "properties must be a non-empty dict"}
        try:
            page = await self.client.retrieve_page(page_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if page.get("error"):
            return page
        page_props = page.get("properties", {})
        unknown = [name for name in properties if name not in page_props]
        if unknown:
            return {"error": True, "code": "INVALID_INPUT",
                    "message": f"Unknown propert{'y' if len(unknown) == 1 else 'ies'} "
                               f"on this page: {unknown}. Available: {sorted(page_props)}"}
        try:
            api_props = {}
            for name, value in properties.items():
                ptype = page_props[name].get("type") if isinstance(page_props[name], dict) else None
                api_props[name] = self._convert_property_value(name, ptype, value)
        except ValueError as e:
            return {"error": True, "code": "INVALID_INPUT", "message": str(e)}
        try:
            result = await self.client.update_page_properties(page_id, api_props)
        except Exception as e:
            return {"error": True, "code": "UPDATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        duration = int((time.monotonic() - start_time) * 1000)
        return {"page_id": result.get("id", page_id),
                "updated": list(api_props.keys()),
                "duration_ms": duration}

    async def archive_page(self, page_id: str, restore: bool = False,
                           object_type: str = "auto") -> dict:
        """Archive (or restore) a page or database.

        Idempotent: archiving an already-archived object still succeeds. When
        object_type is "auto", the pages endpoint is tried first and falls back
        to the databases endpoint on 404 (Notion databases are not pages).
        """
        start_time = time.monotonic()
        archived = not restore
        try:
            result = await self.client.archive_page(page_id, archived, object_type)
        except Exception as e:
            return {"error": True, "code": "UPDATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        object_kind = result.get("object")
        if object_kind not in ("page", "database"):
            object_kind = "database" if object_type == "database" else "page"
        duration = int((time.monotonic() - start_time) * 1000)
        return {"object": object_kind,
                "id": result.get("id", page_id),
                "archived": archived,
                "duration_ms": duration}


    async def write_document(self, parent_page_id: str, title: str,
                              markdown: str, properties: Optional[dict] = None) -> dict:
        start_time = time.monotonic()
        if not markdown or not markdown.strip():
            try:
                parent = {"type": "page_id", "page_id": parent_page_id}
                props = {"title": {"title": [{"type": "text", "text": {"content": title}}]}}
                if properties:
                    props.update(properties)
                page = await self.client.create_page(parent, props)
                if page.get("error"):
                    return page
                return {"page_id": page["id"], "url": page.get("url", ""),
                        "stats": {"total_blocks": 0, "batches": 0, "duration_ms": 0, "rate_limited": False}}
            except Exception as e:
                return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        try:
            blocks = self.converter.convert(markdown)
        except Exception as e:
            return {"error": True, "code": "PARSE_FAILED", "message": f"Failed to parse markdown: {e}"}
        if not blocks:
            try:
                parent = {"type": "page_id", "page_id": parent_page_id}
                props = {"title": {"title": [{"type": "text", "text": {"content": title}}]}}
                if properties:
                    props.update(properties)
                page = await self.client.create_page(parent, props)
                return {"page_id": page["id"], "url": page.get("url", ""),
                        "stats": {"total_blocks": 0, "batches": 0, "duration_ms": 0, "rate_limited": False}}
            except Exception as e:
                return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        # Rule 3: markdown tables must carry their rows inline (the converter
        # does; this guard protects any future converter change).
        missing = find_table_missing_children(blocks)
        if missing:
            path, _ = missing
            return {"error": True, "code": "INVALID_INPUT",
                    "message": (f"Table block at index path {list(path)} has no inline children. "
                                "Tables must be sent with their rows inline.")}
        try:
            parent = {"type": "page_id", "page_id": parent_page_id}
            props = {"title": {"title": [{"type": "text", "text": {"content": title}}]}}
            if properties:
                props.update(properties)
            first_chunk = blocks[:100]
            remaining = blocks[100:]
            page = await self.client.create_page(parent, props, children=first_chunk)
            if page.get("error"):
                return page
            page_id = page["id"]
        except Exception as e:
            return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        stats = {"total_blocks": len(blocks), "batches": 1, "duration_ms": 0, "rate_limited": False}
        if remaining:
            batch_stats = await self.batcher.append_blocks(page_id, remaining)
            stats["batches"] += batch_stats.get("batches", 0)
            if batch_stats.get("rate_limited"):
                stats["rate_limited"] = True
        stats["duration_ms"] = int((time.monotonic() - start_time) * 1000)
        return {"page_id": page_id, "url": page.get("url", ""), "stats": stats}

    async def write_blocks(self, parent_block_id: str, blocks: List[Dict],
                           after: Optional[str] = None) -> dict:
        if not blocks:
            return {"block_count": 0, "batches": 0}
        if len(blocks) > 5000:
            return {"error": True, "code": "TOO_MANY_BLOCKS",
                    "message": "Maximum 5000 blocks per request. Split into multiple calls."}
        for i, block in enumerate(blocks):
            if block.get("type") not in VALID_BLOCK_TYPES:
                return {"error": True, "code": "INVALID_BLOCK",
                        "message": f"Invalid block type '{block.get('type')}' at index {i}. Valid types: {sorted(VALID_BLOCK_TYPES)}"}
        # Rule 3: a table must ship with its rows inline.
        missing = find_table_missing_children(blocks)
        if missing:
            path, _ = missing
            return {"error": True, "code": "INVALID_INPUT",
                    "message": (f"Table block at index path {list(path)} has no inline children. "
                                "Notion rejects this with \"body.children[N].table.children "
                                "should be defined\". Send tables with their rows inline under "
                                "the 'table' object's 'children' array.")}
        # Rule 2: `after` must be a sibling of the append target.
        if after:
            after_err = await self._validate_after(parent_block_id, after)
            if after_err:
                return after_err
        try:
            stats = await self.batcher.append_blocks(parent_block_id, blocks, after=after)
        except Exception as e:
            return {"error": True, "code": "APPEND_FAILED", "message": str(e)}
        result = {"block_count": len(blocks), "batches": stats.get("batches", 1)}
        result.update(await self._verify_append(parent_block_id, blocks,
                                                stats.get("batches", 1), after=after))
        return result

    async def append_to_page(self, page_id: str, markdown: str,
                             after: Optional[str] = None) -> dict:
        """Append markdown content to an existing page.

        Optional ``after`` inserts the new blocks immediately following a
        sibling block on the page.
        """
        start_time = time.monotonic()
        if not markdown or not markdown.strip():
            return {"block_count": 0, "batches": 0, "duration_ms": 0}
        try:
            blocks = self.converter.convert(markdown)
        except Exception as e:
            return {"error": True, "code": "PARSE_FAILED", "message": f"Failed to parse markdown: {e}"}
        if not blocks:
            return {"block_count": 0, "batches": 0, "duration_ms": 0}
        missing = find_table_missing_children(blocks)
        if missing:
            path, _ = missing
            return {"error": True, "code": "INVALID_INPUT",
                    "message": (f"Table block at index path {list(path)} has no inline children. "
                                "Tables must be sent with their rows inline under the 'table' "
                                "object's 'children' array.")}
        if after:
            after_err = await self._validate_after(page_id, after)
            if after_err:
                return after_err
        try:
            stats = await self.batcher.append_blocks(page_id, blocks, after=after)
        except Exception as e:
            return {"error": True, "code": "APPEND_FAILED", "message": str(e)}
        stats["duration_ms"] = int((time.monotonic() - start_time) * 1000)
        result = {"block_count": len(blocks), **stats}
        result.update(await self._verify_append(page_id, blocks, stats.get("batches", 1),
                                                after=after))
        return result

    # --- After-guard / append verification helpers (production rules 2 & 4) ---

    @staticmethod
    def _extract_parent_id(block: dict) -> Optional[str]:
        parent = block.get("parent") or {}
        for key in ("page_id", "block_id", "database_id"):
            value = parent.get(key)
            if value:
                return str(value).replace("-", "").lower()
        return None

    async def _validate_after(self, parent_block_id: str, after: str) -> Optional[dict]:
        """Local guard for the `after` parameter (production rule 2).

        Notion rejects an `after` block that does not share the target's
        parent. Retrieve the block first and allow it only when it is a leaf
        (``has_children`` false) or when its parent matches the append target.
        A missing block returns NOT_FOUND.
        """
        try:
            block = await self.client.retrieve_block(after)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if block.get("error"):
            if block.get("code") == "NOT_FOUND":
                return {"error": True, "code": "NOT_FOUND",
                        "message": (f"after block '{after}' was not found. Verify the block ID "
                                    "and that the integration has access to it.")}
            return block
        if block.get("has_children") is False:
            return None
        parent_id = self._extract_parent_id(block)
        target = str(parent_block_id or "").replace("-", "").lower()
        if parent_id and parent_id == target:
            return None
        return {"error": True, "code": "INVALID_INPUT",
                "message": (f"after block '{after}' does not belong to the target parent "
                            f"('{target}'). A block passed as `after` must share the same parent "
                            "as the append target. When appending to a page, `after` must be a "
                            "direct child of that page, not a block nested inside another block.")}

    async def _verify_append(self, parent_block_id: str, sent_blocks: List[Dict],
                             batches: int, after: Optional[str] = None) -> dict:
        """Verify an append by reading the page tail back once (rule 4).

        Cost control: skipped when the write spanned more than one batch (the
        reason is recorded). Never validates by result count - Notion may
        return more results than blocks sent.

        Skipped when ``after`` is set: the inserted blocks land mid-page, not
        at the tail, so the tail-signature check does not apply and would
        report a false mismatch.
        """
        if after:
            return {"verified": None,
                    "explain": "verification skipped: insertion position via after; "
                               "tail check not applicable"}
        if batches > 1:
            return {"verified": None,
                    "explain": f"verification skipped: append spanned {batches} batches"}
        try:
            response = await self.client.retrieve_block_children(parent_block_id, page_size=100)
        except Exception as e:
            return {"verified": False, "explain": f"read-back failed: {e}"}
        if response.get("error"):
            return {"verified": False,
                    "explain": f"read-back failed: {response.get('message')}"}
        results = response.get("results") or []
        if response.get("has_more"):
            return {"verified": False,
                    "explain": ("read-back could not confirm the page tail: the parent has more "
                                "than one page of children")}
        if len(results) < len(sent_blocks):
            return {"verified": False,
                    "explain": (f"read-back returned {len(results)} blocks but {len(sent_blocks)} "
                                "were sent; cannot confirm the tail")}
        tail = results[-len(sent_blocks):] if sent_blocks else []
        sent_sigs = [block_signature(b) for b in sent_blocks]
        got_sigs = [block_signature(b) for b in tail]
        if sent_sigs == got_sigs:
            return {"verified": True}
        return {"verified": False,
                "explain": (f"block signature mismatch: sent={sent_sigs[:5]} "
                            f"got={got_sigs[:5]}")}

    async def update_block(self, block_id: str, markdown: str) -> dict:
        """Update a block's content from markdown. Works for: paragraph, heading_1-3, quote,
        bulleted_list_item, numbered_list_item, to_do, toggle, callout, code."""
        start_time = time.monotonic()
        if not markdown or not markdown.strip():
            return {"error": True, "code": "INVALID_INPUT", "message": "markdown content is required"}
        try:
            blocks = self.converter.convert(markdown)
        except Exception as e:
            return {"error": True, "code": "PARSE_FAILED", "message": f"Failed to parse markdown: {e}"}
        if not blocks:
            return {"error": True, "code": "PARSE_FAILED", "message": "Markdown produced no blocks"}
        if len(blocks) > 1:
            return {"error": True, "code": "TOO_MANY_BLOCKS",
                    "message": f"Markdown produced {len(blocks)} blocks but notion_update_block updates exactly ONE block. "
                               "Pass a single paragraph/heading/list line, or use notion_append_to_page / notion_write_blocks for multi-block content."}
        block_data = blocks[0]
        block_type = block_data["type"]
        content = {block_type: block_data[block_type]}
        # Notion cannot change a block's type. Fetch the existing block first;
        # if the markdown would change its type, either auto-map plain text onto
        # the existing type (heading/quote/list) or fail with a clear error.
        try:
            existing = await self.client.retrieve_block(block_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if existing.get("error"):
            return existing
        existing_type = existing.get("type", "")
        if existing_type and existing_type != block_type:
            if block_type == "paragraph" and existing_type in (
                "heading_1", "heading_2", "heading_3", "quote",
                "bulleted_list_item", "numbered_list_item", "to_do",
                "toggle", "callout",
            ):
                rich_text = block_data["paragraph"]["rich_text"]
                content = {existing_type: {"rich_text": rich_text}}
                if existing_type == "to_do":
                    content["to_do"]["checked"] = existing.get("to_do", {}).get("checked", False)
                block_type = existing_type
            else:
                return {"error": True, "code": "BLOCK_TYPE_MISMATCH",
                        "message": f"Cannot update block: existing type is '{existing_type}' but markdown converts to '{block_type}'. "
                                   "Notion does not support changing a block's type. Delete + recreate the block, or "
                                   "write markdown that matches the existing type."}
        try:
            result = await self.client.update_block(block_id, content)
        except Exception as e:
            return {"error": True, "code": "UPDATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        duration = int((time.monotonic() - start_time) * 1000)
        return {"block_id": block_id, "type": block_type, "duration_ms": duration}

    async def update_block_raw(self, block_id: str, block_data: List[Dict]) -> dict:
        """Update a block using raw block JSON. Supports all block types including
        callout, toggle, column, synced_block, table, and more."""
        start_time = time.monotonic()
        if not block_data:
            return {"error": True, "code": "INVALID_INPUT", "message": "block_data is required"}
        data = block_data[0] if isinstance(block_data, list) else block_data
        block_type = data.get("type", "")
        if not block_type:
            return {"error": True, "code": "INVALID_INPUT", "message": "block_data must contain a 'type' field"}
        try:
            result = await self.client.update_block(block_id, data)
        except Exception as e:
            return {"error": True, "code": "UPDATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        duration = int((time.monotonic() - start_time) * 1000)
        return {"block_id": block_id, "type": block_type, "duration_ms": duration}

    async def delete_block(self, block_id: str) -> dict:
        """Delete a block by ID. Also removes all child blocks recursively.

        Idempotent: deleting an already-deleted block returns success with
        already_deleted=True instead of an error (observed in production:
        cleanup scripts re-delete blocks and got 404 spam)."""
        start_time = time.monotonic()
        try:
            result = await self.client.delete_block(block_id)
        except Exception as e:
            return {"error": True, "code": "DELETE_FAILED", "message": str(e)}
        if result.get("error"):
            if result.get("code") == "NOT_FOUND":
                return {"deleted": block_id, "already_deleted": True, "duration_ms": 0}
            return result
        duration = int((time.monotonic() - start_time) * 1000)
        return {"deleted": block_id, "duration_ms": duration}

    # --- Table row / table replacement (production rules 5 & 6) ---

    @staticmethod
    def _default_annotations() -> dict:
        return {"bold": False, "italic": False, "strikethrough": False,
                "underline": False, "code": False, "color": "default"}

    @classmethod
    def _normalize_annotations(cls, annotations) -> dict:
        base = cls._default_annotations()
        if isinstance(annotations, dict):
            for key in ("bold", "italic", "strikethrough", "underline", "code"):
                base[key] = bool(annotations.get(key))
            base["color"] = annotations.get("color", "default")
        return base

    @classmethod
    def _cell_annotations(cls, cell) -> dict:
        """Return the annotations of a read-back cell (first rich_text item)."""
        for part in cell or []:
            if isinstance(part, dict) and part.get("annotations"):
                return cls._normalize_annotations(part["annotations"])
        return cls._default_annotations()

    @classmethod
    def _normalize_cell(cls, value, default_annotations=None) -> list:
        """Normalize one cell into Notion ``rich_text[]``.

        Accepts a plain string (keeps ``default_annotations``), a
        ``{"text": ..., "annotations": {...}, "link": ...}`` dict, or an
        explicit rich_text list.
        """
        default_annotations = default_annotations or cls._default_annotations()
        if isinstance(value, str):
            return [{"type": "text", "text": {"content": value},
                     "annotations": dict(default_annotations)}]
        if isinstance(value, dict):
            if "text" in value:
                text_obj = {"content": str(value.get("text"))}
                link = value.get("link")
                if link:
                    text_obj["link"] = {"url": link} if isinstance(link, str) else link
                annotations = (cls._normalize_annotations(value["annotations"])
                               if value.get("annotations") else dict(default_annotations))
                return [{"type": "text", "text": text_obj, "annotations": annotations}]
            if any(k in value for k in ("type", "rich_text")) or "plain_text" in value:
                return [value]
            return [{"type": "text", "text": {"content": str(value)},
                     "annotations": dict(default_annotations)}]
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, str):
                    out.append({"type": "text", "text": {"content": item},
                                "annotations": dict(default_annotations)})
                elif isinstance(item, dict):
                    out.append(item)
            return out or [{"type": "text", "text": {"content": ""},
                            "annotations": dict(default_annotations)}]
        return [{"type": "text", "text": {"content": str(value)},
                 "annotations": dict(default_annotations)}]

    async def update_table_row(self, table_row_block_id: str, cells: list) -> dict:
        """Update a table_row's cells, preserving per-cell annotations.

        Production rule 6: the block is retrieved first so the old cell
        annotations can be preserved. Plain-string cells keep the old
        annotations; ``{"text": ..., "annotations": {...}}`` cells use the
        explicit annotations; a full rich_text list is used as-is.
        """
        start_time = time.monotonic()
        if not isinstance(cells, list) or len(cells) == 0:
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "cells must be a non-empty list"}
        try:
            existing = await self.client.retrieve_block(table_row_block_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if existing.get("error"):
            return existing
        if existing.get("type") != "table_row":
            return {"error": True, "code": "BLOCK_TYPE_MISMATCH",
                    "message": (f"Block '{table_row_block_id}' is type "
                                f"'{existing.get('type')}', not 'table_row'.")}
        old_cells = (existing.get("table_row") or {}).get("cells") or []
        new_cells = []
        for i, cell in enumerate(cells):
            old_annotations = self._cell_annotations(old_cells[i]) if i < len(old_cells) else None
            new_cells.append(self._normalize_cell(cell, old_annotations))
        try:
            result = await self.client.update_block(
                table_row_block_id, {"table_row": {"cells": new_cells}})
        except Exception as e:
            return {"error": True, "code": "UPDATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result
        return {"block_id": table_row_block_id, "type": "table_row",
                "cells": len(new_cells),
                "duration_ms": int((time.monotonic() - start_time) * 1000)}

    async def replace_table(self, table_block_id: str, rows: list) -> dict:
        """Replace a table, allowing a different column count (rule 5).

        Notion cannot change a table's width in place. A new table is created
        with its rows inline immediately after the old one (``after`` = the old
        table's ID), then the old table is deleted. All rows must have the same
        number of cells.
        """
        start_time = time.monotonic()
        if not isinstance(rows, list) or len(rows) == 0:
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "rows must be a non-empty list"}
        try:
            existing = await self.client.retrieve_block(table_block_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if existing.get("error"):
            return existing
        if existing.get("type") != "table":
            return {"error": True, "code": "BLOCK_TYPE_MISMATCH",
                    "message": (f"Block '{table_block_id}' is type "
                                f"'{existing.get('type')}', not 'table'.")}
        columns = len(rows[0]) if isinstance(rows[0], list) else 0
        if not columns:
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "Each row must be a non-empty list of cells"}
        for i, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != columns:
                count = len(row) if isinstance(row, list) else "non-list"
                return {"error": True, "code": "INVALID_INPUT",
                        "message": (f"Row {i} has {count} cells; all rows must have the "
                                    f"same column count ({columns}).")}
        new_rows = [{"object": "block", "type": "table_row",
                     "table_row": {"cells": [self._normalize_cell(c) for c in row]}}
                    for row in rows]
        old_body = existing.get("table") or {}
        new_table = {"object": "block", "type": "table",
                     "table": {"table_width": columns,
                               "has_column_header": bool(old_body.get("has_column_header")),
                               "has_row_header": bool(old_body.get("has_row_header")),
                               "children": new_rows}}
        parent_id = self._extract_parent_id(existing)
        if not parent_id:
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "Could not determine the table's parent; cannot insert the replacement."}
        try:
            response = await self.client.append_block_children(parent_id, [new_table],
                                                               after=table_block_id)
        except Exception as e:
            return {"error": True, "code": "APPEND_FAILED", "message": str(e)}
        if response.get("error"):
            return response
        created = response.get("results") or []
        new_table_id = created[0].get("id") if created and isinstance(created[0], dict) else None
        if not new_table_id:
            return {"error": True, "code": "APPEND_FAILED",
                    "message": "New table was created but its ID was not returned; old table left intact."}
        deleted = await self.delete_block(table_block_id)
        if deleted.get("error"):
            return deleted
        verified_rows = None
        try:
            check = await self.client.retrieve_block_children(new_table_id, page_size=100)
            if not check.get("error"):
                verified_rows = len(check.get("results") or [])
        except Exception:
            verified_rows = None
        return {"old_table_id": table_block_id, "new_table_id": new_table_id,
                "rows": len(rows), "columns": columns,
                "verified_rows": verified_rows,
                "duration_ms": int((time.monotonic() - start_time) * 1000)}

    # --- Page duplication (production rules 1, 3, 4) ---

    async def duplicate_page(self, page_id: str, title: Optional[str] = None) -> dict:
        """Duplicate a page: content tree plus copyable properties.

        Pattern adapted from the production-proven duplicate_page_v4:
        read the whole source tree, create a page under the same parent, send
        blocks sequentially with tables carrying their rows inline (rule 3),
        then verify by reading back and comparing block signatures (rule 4).
        Read-only properties are skipped. On a signature mismatch the created
        page is kept and ``verified`` is false with an ``explain``.
        """
        start_time = time.monotonic()
        try:
            source = await self.client.retrieve_page(page_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if source.get("error"):
            return source
        parent = source.get("parent") or {}
        if not parent.get("type"):
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "Source page has no parent; cannot determine where to create the copy."}
        try:
            tree = await self._read_block_tree(page_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED",
                    "message": f"Failed to read source blocks: {e}"}
        properties = self._copyable_properties(source.get("properties", {}), title)
        try:
            new_page = await self.client.create_page(parent, properties)
        except Exception as e:
            return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        if new_page.get("error"):
            return new_page
        new_page_id = new_page.get("id", "")
        try:
            copied = await self._send_tree(new_page_id, tree)
        except Exception as e:
            return {"error": True, "code": "APPEND_FAILED", "message": str(e)}
        result = {"page_id": new_page_id, "url": new_page.get("url", ""),
                  "blocks_copied": copied,
                  "duration_ms": int((time.monotonic() - start_time) * 1000)}
        try:
            new_tree = await self._read_block_tree(new_page_id)
        except Exception as e:
            result.update({"verified": False, "explain": f"read-back failed: {e}"})
            return result
        src_sigs = [block_signature(n["block"]) for n in _flatten_tree(tree)]
        got_sigs = [block_signature(n["block"]) for n in _flatten_tree(new_tree)]
        verified = src_sigs == got_sigs
        result.update({"verified": verified,
                       "source_blocks": len(src_sigs),
                       "copied_blocks": len(got_sigs)})
        if not verified:
            result["explain"] = (
                f"block signature mismatch after copy: source had {len(src_sigs)} blocks, "
                f"copy has {len(got_sigs)}; the created page was kept")
        return result

    async def _read_block_tree(self, parent_id: str, seen=None) -> list:
        """Read a block subtree recursively (pagination + depth).

        Returns nodes shaped ``{"block": <raw block>, "children": [...]}``.
        Recursion is skipped for child pages, child databases and synced
        blocks; ``seen`` guards against cycles.
        """
        if seen is None:
            seen = set()
        if parent_id in seen:
            return []
        seen.add(parent_id)
        nodes = []
        cursor = None
        while True:
            try:
                response = await self.client.retrieve_block_children(
                    parent_id, page_size=100, start_cursor=cursor)
            except Exception as e:
                raise RuntimeError(str(e))
            if not isinstance(response, dict) or response.get("error"):
                message = response.get("message") if isinstance(response, dict) else "invalid response"
                raise RuntimeError(message or "retrieve_block_children failed")
            for block in response.get("results", []) or []:
                node = {"block": block, "children": []}
                nodes.append(node)
                if (block.get("has_children")
                        and block.get("type") not in ("child_page", "child_database", "synced_block")):
                    node["children"] = await self._read_block_tree(block.get("id"), seen)
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
            if not cursor:
                break
        return nodes

    async def _send_tree(self, target_id: str, nodes: list) -> int:
        """Send one tree level sequentially, then recurse into created parents.

        Returns the number of source blocks sent. Results are mapped to nodes
        by index for the first ``len(chunk)`` entries; the response is never
        validated by count (rule 4).
        """
        if not nodes:
            return 0
        sendable = []
        consumed_ids = set()
        for node in nodes:
            payload = self._copy_payload(node)
            if payload is None:
                continue
            sendable.append((node, payload))
            if node["block"].get("type") in _INLINE_CHILD_TYPES:
                for child in node["children"]:
                    consumed_ids.add(child["block"].get("id"))
        sent = 0
        next_frontier = []
        for i in range(0, len(sendable), 100):
            chunk = sendable[i:i + 100]
            response = await self.client.append_block_children(
                target_id, [payload for _, payload in chunk])
            if not isinstance(response, dict) or response.get("error"):
                message = response.get("message") if isinstance(response, dict) else "invalid response"
                raise RuntimeError(message or "append failed")
            created = response.get("results") or []
            sent += len(chunk)
            for j, (node, _payload) in enumerate(chunk):
                creator = created[j] if j < len(created) else None
                new_id = creator.get("id") if isinstance(creator, dict) else None
                if not new_id:
                    continue
                kids = [c for c in node["children"] if c["block"].get("id") not in consumed_ids]
                if kids:
                    next_frontier.append((new_id, kids))
        for new_id, kids in next_frontier:
            sent += await self._send_tree(new_id, kids)
        return sent

    def _copy_payload(self, node: dict) -> Optional[dict]:
        """Build a writable payload for a source block (None if unsupported)."""
        block = node["block"]
        block_type = block.get("type")
        data = block.get(block_type) or {}
        rich_types = {"heading_1", "heading_2", "heading_3", "paragraph",
                      "bulleted_list_item", "numbered_list_item", "quote",
                      "to_do", "toggle", "code", "callout"}
        if block_type in rich_types:
            body = {"rich_text": self._copy_rich_text(data.get("rich_text"))}
            if block_type == "to_do":
                body["checked"] = bool(data.get("checked"))
            if block_type == "code":
                body["language"] = data.get("language", "plain text")
                body["caption"] = self._copy_rich_text(data.get("caption"))
            if block_type == "callout":
                if data.get("icon"):
                    body["icon"] = data["icon"]
                body["color"] = data.get("color", "default")
            return {"object": "block", "type": block_type, block_type: body}
        if block_type == "divider":
            return {"object": "block", "type": block_type, block_type: {}}
        if block_type == "table":
            rows = []
            for child in node["children"]:
                if child["block"].get("type") == "table_row":
                    row_payload = self._copy_payload(child)
                    if row_payload:
                        rows.append(row_payload)
            width = int(data.get("table_width") or 0)
            if not rows:
                width = width or 1
                rows = [{"object": "block", "type": "table_row",
                         "table_row": {"cells": [[{"type": "text", "text": {"content": ""}}]
                                                for _ in range(width)]}}]
            elif not width:
                width = len(rows[0]["table_row"]["cells"])
            return {"object": "block", "type": block_type,
                    "table": {"table_width": width,
                              "has_column_header": bool(data.get("has_column_header")),
                              "has_row_header": bool(data.get("has_row_header")),
                              "children": rows}}
        if block_type == "table_row":
            return {"object": "block", "type": block_type,
                    "table_row": {"cells": [self._copy_rich_text(c)
                                            for c in data.get("cells") or []]}}
        if block_type in ("image", "bookmark", "embed", "video", "audio",
                          "file", "pdf", "equation", "link_preview"):
            return {"object": "block", "type": block_type, block_type: dict(data)}
        # child_page, child_database, synced_block, template, ... are skipped.
        return None

    @staticmethod
    def _copy_rich_text(parts) -> list:
        """Convert a read-back rich_text array into a writable rich_text array."""
        out = []
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "text")
            item = {"type": ptype}
            if ptype == "text":
                text: dict = {"content": part.get("plain_text", "")}
                href = part.get("href")
                if href:
                    text["link"] = {"url": href}
                item["text"] = text
            elif ptype == "mention":
                if not part.get("mention"):
                    continue
                item["mention"] = part["mention"]
                item["plain_text"] = part.get("plain_text", "")
            elif ptype == "equation":
                if not part.get("equation"):
                    continue
                item["equation"] = part["equation"]
            else:
                continue
            item["annotations"] = WriteHandler._normalize_annotations(part.get("annotations"))
            out.append(item)
        if not out:
            out = [{"type": "text", "text": {"content": ""},
                    "annotations": WriteHandler._default_annotations()}]
        return out

    def _copyable_properties(self, source_props: dict, title: Optional[str]) -> dict:
        """Copy writable page properties for a duplicate.

        Title becomes ``title`` when given, otherwise ``"<source title> (copy)"``.
        Read-only and unsupported property types are skipped.
        """
        properties: dict = {}
        for name, spec in (source_props or {}).items():
            if not isinstance(spec, dict):
                continue
            ptype = spec.get("type")
            if ptype not in COPYABLE_PROPERTY_TYPES:
                continue
            value = spec.get(ptype)
            if ptype == "title":
                text = title if title is not None else (_plain_text(value) + " (copy)")
                properties[name] = {"title": [{"type": "text", "text": {"content": text}}]}
            elif ptype == "rich_text":
                properties[name] = {"rich_text": self._copy_rich_text(value)}
            elif ptype in ("number", "url", "email", "phone_number", "checkbox"):
                if value is not None:
                    properties[name] = {ptype: value}
            elif ptype in ("select", "status"):
                if isinstance(value, dict) and value.get("name"):
                    properties[name] = {ptype: {"name": value["name"]}}
            elif ptype == "multi_select":
                properties[name] = {"multi_select": [
                    {"name": item["name"]} for item in (value or [])
                    if isinstance(item, dict) and item.get("name")]}
            elif ptype == "date":
                if isinstance(value, dict) and value.get("start"):
                    properties[name] = {"date": {
                        key: value[key] for key in ("start", "end", "time_zone")
                        if value.get(key)}}
            elif ptype == "relation":
                properties[name] = {"relation": [
                    {"id": item["id"]} for item in (value or [])
                    if isinstance(item, dict) and item.get("id")]}
        if title is not None and not any("title" in v for v in properties.values()):
            properties["title"] = {"title": [{"type": "text", "text": {"content": title}}]}
        return properties
