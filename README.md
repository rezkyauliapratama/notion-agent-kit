# notion-agent-kit

Custom Notion MCP server for Hermes Agent — full read/write support for all Notion block types with enterprise-grade edge case handling and performance.

## Quick Start

```bash
pip install -r requirements.txt
export NOTION_TOKEN="ntn_..."
python server.py
```

## Tools

| Tool | Description |
|------|-------------|
| `notion_find` | Search pages/databases by title |
| `notion_read_page` | Read page content + metadata |
| `notion_write_document` | Write full document from markdown |
| `notion_write_blocks` | Write raw Notion block JSON |
| `notion_inspect_database` | Inspect database schema |

## Design

See [GRAND_DESIGN.md](GRAND_DESIGN.md) for full architecture documentation.
