"""Input validation for all MCP tools."""

import re
from typing import Optional

PAGE_ID_PATTERN = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.IGNORECASE)
PAGE_ID_PATTERN_NO_HYPHEN = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
URL_ID_PATTERN = re.compile(r"[a-f0-9]{32}", re.IGNORECASE)

# Keys that look like an object ID or a Notion URL. Anything else (page title,
# emoji, random text) is rejected early with a helpful message.
_ID_LIKE_HINTS = ("notion.so", "notion.site", "http://", "https://", "/")


def extract_page_id(page_id) -> Optional[str]:
    """Normalize a raw page/database identifier.

    Accepts:
    - UUID with dashes:      3aa0f42d-7035-802b-bf61-e7effd583a39
    - 32 hex chars:          3aa0f42d7035802bbf61e7effd583a39
    - Notion URL:            https://www.notion.so/xyz-3aa0f42d7035802bbf61e7effd583a39
                             https://app.notion.com/p/Title-3aa0f42d7035802bbf61e7effd583a39

    Returns the normalized ID (32 hex chars) or None if the input is not an ID/URL.
    """
    if not page_id or not isinstance(page_id, str):
        return None
    s = page_id.strip()
    if not s:
        return None
    if PAGE_ID_PATTERN.match(s):
        return s.replace("-", "").lower()
    if PAGE_ID_PATTERN_NO_HYPHEN.match(s):
        return s.lower()
    # URL: the object ID is the LAST 32-hex token (page titles may contain hex runs).
    if any(hint in s for hint in _ID_LIKE_HINTS):
        candidates = URL_ID_PATTERN.findall(s.lower())
        if candidates:
            return candidates[-1]
    return None


def validate_page_id(page_id: str) -> Optional[dict]:
    """Legacy validator: returns an error dict for invalid IDs, else None.

    Prefer extract_page_id() in handlers - it also resolves Notion URLs.
    """
    if extract_page_id(page_id) is None:
        return {"error": True, "code": "INVALID_INPUT",
                "message": f"Invalid page/database ID. Expected 32 hex chars, a UUID, or a Notion URL. Got: '{page_id}'"}
    return None
