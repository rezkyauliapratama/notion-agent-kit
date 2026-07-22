"""notion-agent-kit - MCP Server Entrypoint"""

import os
import asyncio
import logging
from typing import Optional

from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio

from core.notion_client import NotionClient
from core.schema_cache import SchemaCache
from core.block_converter import MarkdownConverter
from core.block_batcher import BlockBatcher
from handlers.find_handler import FindHandler
from handlers.read_handler import ReadHandler
from handlers.write_handler import WriteHandler
from handlers.validate import validate_page_id

logger = logging.getLogger("notion-agent-kit")


class NotionAgentKitServer:
    """Main MCP server for Notion integration."""

    def __init__(self):
        token = os.environ.get("NOTION_TOKEN")
        if not token:
            raise ValueError(
                "NOTION_TOKEN environment variable is required. "
                "Set it in .env or export NOTION_TOKEN=ntn_..."
            )

        self.client = NotionClient(token=token)
        self.cache = SchemaCache()
        self.converter = MarkdownConverter()
        self.batcher = BlockBatcher(client=self.client)
        self.find_handler = FindHandler(client=self.client)
        self.read_handler = ReadHandler(client=self.client, max_depth=2)
        self.write_handler = WriteHandler(
            client=self.client, converter=self.converter, batcher=self.batcher
        )
        self.server = Server("notion-agent-kit")
        self._register_tools()

    def _register_tools(self):
        @self.server.tool("notion_find")
        async def notion_find(query: str, object_type: Optional[str] = None, page_size: int = 10) -> dict:
            """Search for Notion pages or databases by title."""
            return await self.find_handler.find(query, object_type, page_size)

        @self.server.tool("notion_read_page")
        async def notion_read_page(page_id: str, max_blocks: int = 200, depth: int = 2) -> dict:
            """Read a Notion page's content and metadata."""
            validation = validate_page_id(page_id)
            if validation:
                return validation
            return await self.read_handler.read_page(page_id, max_blocks, depth)

        @self.server.tool("notion_write_document")
        async def notion_write_document(parent_page_id: str, title: str, markdown: str, properties: Optional[dict] = None) -> dict:
            """Write a full document from markdown into a new Notion page."""
            validation = validate_page_id(parent_page_id)
            if validation:
                return validation
            return await self.write_handler.write_document(parent_page_id, title, markdown, properties)

        @self.server.tool("notion_write_blocks")
        async def notion_write_blocks(parent_block_id: str, blocks: list) -> dict:
            """Write raw Notion block JSON to a page."""
            validation = validate_page_id(parent_block_id)
            if validation:
                return validation
            return await self.write_handler.write_blocks(parent_block_id, blocks)

        @self.server.tool("notion_inspect_database")
        async def notion_inspect_database(database_id: str) -> dict:
            """Inspect a Notion database schema."""
            validation = validate_page_id(database_id)
            if validation:
                return validation
            cached = self.cache.get(database_id)
            if cached:
                return cached
            result = await self.read_handler.inspect_database(database_id)
            if "error" not in result:
                self.cache.set(database_id, result)
            return result


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    server = NotionAgentKitServer()
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="notion-agent-kit",
                server_version="1.0.0",
                capabilities=server.server.get_capabilities(
                    notification_options=NotificationOptions(), experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())
