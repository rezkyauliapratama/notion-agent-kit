"""Input validation for all MCP tools."""

import re
from typing import Optional

PAGE_ID_PATTERN = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.IGNORECASE)
PAGE_ID_PATTERN_NO_HYPHEN = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)


def validate_page_id(page_id: str) -> Optional[dict]:
    if not page_id or not isinstance(page_id, str):
        return {"error": True, "code": "INVALID_INPUT", "message": "page_id is required and must be a string."}
    if PAGE_ID_PATTERN.match(page_id) or PAGE_ID_PATTERN_NO_HYPHEN.match(page_id):
        return None
    return {"error": True, "code": "INVALID_INPUT",
            "message": f"Invalid page/database ID format. Expected 32 hex characters (8-4-4-4-12). Got: '{page_id}'"}
