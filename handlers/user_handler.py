"""User handler - retrieve the token bot, one user, or a paginated user list."""

from typing import Optional

from handlers.validate import extract_user_id


class UserHandler:
    """Handle notion_get_self, notion_get_user, and notion_get_users tools."""

    def __init__(self, client):
        self.client = client

    async def get_self(self) -> dict:
        """Return a safe, stable projection of the token's Notion user."""
        try:
            result = await self.client.get_self()
        except Exception as e:
            return self._fetch_failed(str(e))
        return self._single_user_result(result)

    async def get_user(self, user_id: str) -> dict:
        """Retrieve one user by a raw UUID, never by a page URL."""
        normalized_user_id = extract_user_id(user_id)
        if normalized_user_id is None:
            return self._invalid_input(
                "Invalid user_id. Expected 32 hex chars or a UUID; Notion page URLs are not user IDs.")
        try:
            result = await self.client.retrieve_user(normalized_user_id)
        except Exception as e:
            return self._fetch_failed(str(e))
        return self._single_user_result(result, fallback_id=normalized_user_id)

    async def get_users(self, page_size: int = 100,
                        start_cursor: Optional[str] = None) -> dict:
        """List one page of users while preserving Notion pagination metadata."""
        if isinstance(page_size, bool) or not isinstance(page_size, int):
            return self._invalid_input("page_size must be an integer")
        if page_size < 1:
            page_size = 1
        if page_size > 100:
            page_size = 100
        if start_cursor is not None:
            if not isinstance(start_cursor, str) or not start_cursor.strip():
                return self._invalid_input(
                    "start_cursor must be a non-empty string when supplied")

        try:
            if start_cursor is None:
                result = await self.client.list_users(page_size)
            else:
                result = await self.client.list_users(page_size, start_cursor)
        except Exception as e:
            return self._fetch_failed(str(e))

        if not isinstance(result, dict):
            return self._fetch_failed("Notion returned an invalid users response")
        if result.get("error"):
            return result

        raw_users = result.get("results")
        if not isinstance(raw_users, list) or any(
                not isinstance(user, dict) for user in raw_users):
            return self._fetch_failed("Notion returned an invalid users response")

        has_more = result.get("has_more", False)
        if not isinstance(has_more, bool):
            return self._fetch_failed("Notion returned invalid users pagination metadata")
        next_cursor = result.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            return self._fetch_failed("Notion returned invalid users pagination metadata")

        return {
            "results": [self._format_user(user) for user in raw_users],
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    def _single_user_result(self, result, fallback_id: Optional[str] = None) -> dict:
        if not isinstance(result, dict):
            return self._fetch_failed("Notion returned an invalid user response")
        if result.get("error"):
            return result
        return self._format_user(result, fallback_id=fallback_id)

    @classmethod
    def _format_user(cls, user: dict, fallback_id: Optional[str] = None) -> dict:
        """Allowlist user fields so raw API payloads cannot expose credentials."""
        person = user.get("person")
        if not isinstance(person, dict):
            person = {}
        bot = user.get("bot")
        if not isinstance(bot, dict):
            bot = None

        user_id = user.get("id")
        if not isinstance(user_id, str) or not user_id:
            user_id = fallback_id or ""

        email = person.get("email")
        if not isinstance(email, str):
            email = None
        email_verified = person.get("email_verified")
        if not isinstance(email_verified, bool):
            email_verified = None

        return {
            "id": user_id,
            "name": cls._optional_string(user.get("name")),
            "avatar_url": cls._optional_string(user.get("avatar_url")),
            "type": cls._optional_string(user.get("type")),
            "email": email,
            "email_verified": email_verified,
            "bot": cls._format_bot(bot),
        }

    @classmethod
    def _format_bot(cls, bot: Optional[dict]) -> Optional[dict]:
        if bot is None:
            return None
        owner = bot.get("owner")
        if not isinstance(owner, dict):
            owner = {}
        return {
            "owner_type": cls._optional_string(owner.get("type")),
            "workspace_name": cls._optional_string(bot.get("workspace_name")),
            "workspace_id": cls._optional_string(bot.get("workspace_id")),
        }

    @staticmethod
    def _optional_string(value) -> Optional[str]:
        return value if isinstance(value, str) else None

    @staticmethod
    def _invalid_input(message: str) -> dict:
        return {"error": True, "code": "INVALID_INPUT", "message": message}

    @staticmethod
    def _fetch_failed(message: str) -> dict:
        return {"error": True, "code": "FETCH_FAILED", "message": message}
