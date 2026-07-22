"""Block type definitions and validators."""

SUPPORTED_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout",
    "code", "divider", "table_of_contents", "breadcrumb", "column", "column_list",
    "table", "table_row", "equation",
    "image", "video", "file", "pdf", "audio", "embed", "bookmark", "link_preview",
    "synced_block", "template", "link_to_page", "child_page", "child_database",
}

CONTAINER_BLOCK_TYPES = {
    "paragraph", "bulleted_list_item", "numbered_list_item", "to_do",
    "toggle", "quote", "callout", "synced_block", "column", "column_list", "table",
}


def validate_block_type(block_type: str) -> bool:
    return block_type in SUPPORTED_BLOCK_TYPES


def can_have_children(block_type: str) -> bool:
    return block_type in CONTAINER_BLOCK_TYPES
