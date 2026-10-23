"""Comment handler - create and list Notion comments."""

from typing import Optional

# Notion limits an individual rich_text object's text.content to 2,000 chars
# and a rich_text array to 100 objects.
RICH_TEXT_MAX = 2000
MAX_RICH_TEXT_ITEMS = 100


class CommentHandler:
    """Handle notion_create_comment and notion_get_comments tools."""

    def __init__(self, client):
        self.client = client

    async def create_comment(self, text: str, page_id: Optional[str] = None,
                             block_id: Optional[str] = None,
                             discussion_id: Optional[str] = None) -> dict:
        """Create a page/block comment or reply to one discussion thread.

        IDs are normalized at the MCP boundary. This handler validates the
        target combination and turns plain text into Notion rich_text.
        """
        if not isinstance(text, str) or not text.strip():
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "text is required"}

        targets = [("page_id", page_id), ("block_id", block_id),
                   ("discussion_id", discussion_id)]
        provided = [(kind, value) for kind, value in targets if value is not None]
        if len(provided) != 1:
            return {
                "error": True,
                "code": "INVALID_INPUT",
                "message": ("Provide exactly one of page_id, block_id, or "
                            "discussion_id."),
            }
        target_kind, target_id = provided[0]
        if not isinstance(target_id, str) or not target_id.strip():
            return {"error": True, "code": "INVALID_INPUT",
                    "message": f"{target_kind} is required"}

        rich_text = self._build_rich_text(text)
        if len(rich_text) > MAX_RICH_TEXT_ITEMS:
            return {
                "error": True,
                "code": "INVALID_INPUT",
                "message": (f"text is too long: comments support at most "
                            f"{RICH_TEXT_MAX * MAX_RICH_TEXT_ITEMS} characters."),
            }

        parent = None
        if target_kind in ("page_id", "block_id"):
            parent = {"type": target_kind, target_kind: target_id}

        try:
            if target_kind == "discussion_id":
                result = await self.client.create_comment(
                    rich_text, discussion_id=target_id)
            else:
                result = await self.client.create_comment(rich_text, parent=parent)
        except Exception as e:
            return {"error": True, "code": "CREATE_FAILED", "message": str(e)}
        if result.get("error"):
            return result

        comment = self._format_comment(result)
        return {
            "comment_id": comment["id"],
            "discussion_id": comment["discussion_id"] or (
                target_id if target_kind == "discussion_id" else ""),
            "parent": comment["parent"] or (parent or {}),
            "text": comment["text"] or text,
            "rich_text": comment["rich_text"] or rich_text,
            "created": comment["created"],
            "last_edited": comment["last_edited"],
            "created_by": comment["created_by"],
        }

    async def get_comments(self, block_id: str, page_size: int = 100) -> dict:
        """List comments attached to a page or block.

        The client currently exposes one page at a time, so this preserves
        Notion's has_more and next_cursor values for callers.
        """
        if not isinstance(block_id, str) or not block_id.strip():
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "block_id is required"}
        if isinstance(page_size, bool) or not isinstance(page_size, int):
            return {"error": True, "code": "INVALID_INPUT",
                    "message": "page_size must be an integer"}
        if page_size < 1:
            page_size = 1
        if page_size > 100:
            page_size = 100

        try:
            result = await self.client.retrieve_comments(block_id, page_size)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}
        if result.get("error"):
            return result

        raw_comments = result.get("results", [])
        if not isinstance(raw_comments, list):
            raw_comments = []
        comments = [self._format_comment(comment) for comment in raw_comments]
        return {
            "block_id": block_id,
            "total": len(comments),
            "has_more": result.get("has_more", False),
            "next_cursor": result.get("next_cursor"),
            "comments": comments,
        }

    @staticmethod
    def _build_rich_text(text: str) -> list:
        return [
            {"type": "text", "text": {"content": text[i:i + RICH_TEXT_MAX]}}
            for i in range(0, len(text), RICH_TEXT_MAX)
        ]

    @classmethod
    def _format_comment(cls, comment: dict) -> dict:
        if not isinstance(comment, dict):
            comment = {}
        rich_text = comment.get("rich_text", [])
        if not isinstance(rich_text, list):
            rich_text = []
        parent = comment.get("parent", {})
        if not isinstance(parent, dict):
            parent = {}
        created_by = comment.get("created_by", {})
        if not isinstance(created_by, dict):
            created_by = {}
        return {
            "id": comment.get("id", ""),
            "discussion_id": comment.get("discussion_id", ""),
            "parent": parent,
            "created": comment.get("created_time", ""),
            "last_edited": comment.get("last_edited_time", ""),
            "created_by": created_by,
            "text": cls._plain_text(rich_text),
            "rich_text": rich_text,
        }

    @staticmethod
    def _plain_text(rich_text: list) -> str:
        text = []
        for part in rich_text:
            if not isinstance(part, dict):
                continue
            plain_text = part.get("plain_text")
            if isinstance(plain_text, str):
                text.append(plain_text)
                continue
            text_obj = part.get("text")
            if isinstance(text_obj, dict):
                content = text_obj.get("content")
                if isinstance(content, str):
                    text.append(content)
        return "".join(text)
