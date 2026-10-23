"""Input validation for all MCP tools."""

import re
from typing import Optional

UUID_PATTERN = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.IGNORECASE)
UUID_PATTERN_NO_HYPHEN = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
# Keep the historical names for page-ID validation callers; UUID syntax is
# shared by pages, blocks, databases, and users.
PAGE_ID_PATTERN = UUID_PATTERN
PAGE_ID_PATTERN_NO_HYPHEN = UUID_PATTERN_NO_HYPHEN
URL_ID_PATTERN = re.compile(r"[a-f0-9]{32}", re.IGNORECASE)

# Keys that look like an object ID or a Notion URL. Anything else (page title,
# emoji, random text) is rejected early with a helpful message.
_ID_LIKE_HINTS = ("notion.so", "notion.site", "http://", "https://", "/")


def extract_uuid(value) -> Optional[str]:
    """Normalize a raw Notion UUID without accepting URLs.

    User IDs are UUIDs too, but a page URL can contain a page UUID that is not
    a user ID. Keep URL parsing in ``extract_page_id`` so user-oriented tools
    never accidentally send a page ID to ``/users/{user_id}``.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if UUID_PATTERN.match(value):
        return value.replace("-", "").lower()
    if UUID_PATTERN_NO_HYPHEN.match(value):
        return value.lower()
    return None


def extract_user_id(user_id) -> Optional[str]:
    """Normalize a raw user UUID; Notion page URLs are intentionally rejected."""
    return extract_uuid(user_id)


def extract_page_id(page_id) -> Optional[str]:
    """Normalize a raw page/database identifier.

    Accepts:
    - UUID with dashes:      3aa0f42d-7035-802b-bf61-e7effd583a39
    - 32 hex chars:          3aa0f42d7035802bbf61e7effd583a39
    - Notion URL:            https://www.notion.so/xyz-3aa0f42d7035802bbf61e7effd583a39
                             https://app.notion.com/p/Title-3aa0f42d7035802bbf61e7effd583a39

    Returns the normalized ID (32 hex chars) or None if the input is not an ID/URL.
    """
    normalized = extract_uuid(page_id)
    if normalized is not None:
        return normalized
    if not page_id or not isinstance(page_id, str):
        return None
    s = page_id.strip()
    if not s:
        return None
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
