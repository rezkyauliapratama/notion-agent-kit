"""Markdown to Notion block JSON converter."""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


class MarkdownConverter:
    """Convert markdown text to Notion block JSON."""

    BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*(?!\*)")
    ITALIC_PATTERN = re.compile(r"\*(.+?)\*(?!\*)")
    CODE_PATTERN = re.compile(r"`(.+?)`")
    LINK_PATTERN = re.compile(r"\[(.+?)\]\((.+?)\)")

    def convert(self, markdown: str) -> List[Dict[str, Any]]:
        if not markdown or not markdown.strip():
            return []
        blocks = []
        lines = markdown.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            # Code block
            if stripped.startswith("```"):
                lang = stripped[3:].strip() or "plain text"
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                i += 1
                blocks.append(self._build_code_block("\n".join(code_lines), lang))
                continue
            # Divider
            if re.match(r"^---+", stripped):
                blocks.append({"type": "divider", "divider": {}})
                i += 1
                continue
            # Heading
            heading = self._parse_heading(stripped)
            if heading:
                block_type, text = heading
                rich_text = self._parse_inline_formatting(text)
                blocks.append({"type": block_type, block_type: {"rich_text": rich_text}})
                i += 1
                continue
            # Quote
            if stripped.startswith("> "):
                rich_text = self._parse_inline_formatting(stripped[2:])
                blocks.append({"type": "quote", "quote": {"rich_text": rich_text}})
                i += 1
                continue
            # To-do
            todo = self._parse_todo(stripped)
            if todo:
                checked, text = todo
                rich_text = self._parse_inline_formatting(text)
                blocks.append({"type": "to_do", "to_do": {"rich_text": rich_text, "checked": checked}})
                i += 1
                continue
            # Bullet list
            if stripped.startswith("- "):
                items, i = self._collect_list(lines, i, "- ")
                for text in items:
                    rich_text = self._parse_inline_formatting(text)
                    blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich_text}})
                continue
            # Numbered list
            if re.match(r"^\d+\. ", stripped):
                items, i = self._collect_list(lines, i, None)
                for text in items:
                    rich_text = self._parse_inline_formatting(text)
                    blocks.append({"type": "numbered_list_item", "numbered_list_item": {"rich_text": rich_text}})
                continue
            # Table
            if stripped.startswith("|"):
                table_blocks, i = self._parse_table(lines, i)
                blocks.extend(table_blocks)
                continue
            # Paragraph
            rich_text = self._parse_inline_formatting(stripped)
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": rich_text}})
            i += 1
        return blocks

    def _parse_heading(self, line: str) -> Optional[Tuple[str, str]]:
        match = re.match(r"^(#{1,3})\s+(.+)$", line)
        if match:
            return (f"heading_{len(match.group(1))}", match.group(2).strip())
        return None

    def _parse_todo(self, line: str) -> Optional[Tuple[bool, str]]:
        match = re.match(r"^- \[([ x])\] (.+)$", line)
        if match:
            return (match.group(1) == "x", match.group(2).strip())
        return None

    def _collect_list(self, lines: List[str], start: int, prefix: Optional[str]) -> Tuple[List[str], int]:
        items = []
        i = start
        while i < len(lines):
            stripped = lines[i].strip()
            if prefix and stripped.startswith(prefix):
                items.append(stripped[len(prefix):].strip())
                i += 1
            elif re.match(r"^\d+\. ", stripped):
                items.append(re.sub(r"^\d+\. ", "", stripped))
                i += 1
            else:
                break
        return items, i

    def _parse_table(self, lines: List[str], start: int) -> Tuple[List[Dict], int]:
        i = start
        if i >= len(lines):
            return [], i
        header_line = lines[i].strip()
        headers = [h.strip() for h in header_line.split("|") if h.strip()]
        if not headers:
            return [], i + 1
        num_columns = len(headers)
        i += 1
        # Skip separator row
        if i < len(lines) and re.match(r"^\|[-:|\s]+\|$", lines[i].strip()):
            i += 1
        table = {"type": "table", "table": {"table_width": num_columns, "has_column_header": True, "children": []}}
        header_row = self._build_table_row(headers, bold=True)
        table["table"]["children"].append(header_row)
        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped.startswith("|"):
                break
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            while len(cells) < num_columns:
                cells.append("")
            cells = cells[:num_columns]
            table["table"]["children"].append(self._build_table_row(cells, bold=False))
            i += 1
        return [table], i

    def _build_table_row(self, cells: List[str], bold: bool = False) -> Dict:
        notion_cells = []
        for cell in cells:
            rich_text = self._parse_inline_formatting(cell)
            if bold:
                for rt in rich_text:
                    rt["annotations"]["bold"] = True
            notion_cells.append(rich_text)
        return {"type": "table_row", "table_row": {"cells": notion_cells}}

    def _build_code_block(self, code: str, language: str) -> Dict:
        return {"type": "code", "code": {"rich_text": [{"type": "text", "text": {"content": code}}], "language": language}}

    def _parse_inline_formatting(self, text: str) -> List[Dict]:
        if not text:
            return [{"type": "text", "text": {"content": ""}, "annotations": {"bold": False, "italic": False, "strikethrough": False, "underline": False, "code": False, "color": "default"}}]
        tokens = self._tokenize(text)
        rich_texts = []
        for token_type, token_text, link_url in tokens:
            if not token_text:
                continue
            base = {"type": "text", "text": {"content": token_text},
                    "annotations": {"bold": False, "italic": False, "strikethrough": False,
                                    "underline": False, "code": False, "color": "default"}}
            if token_type == "bold":
                base["annotations"]["bold"] = True
            elif token_type == "italic":
                base["annotations"]["italic"] = True
            elif token_type == "code":
                base["annotations"]["code"] = True
            elif token_type == "link" and link_url:
                base["text"]["link"] = {"url": link_url}
            rich_texts.append(base)
        return rich_texts

    def _tokenize(self, text: str) -> List[Tuple[str, str, Optional[str]]]:
        tokens = []
        remaining = text
        while remaining:
            best_match = None
            best_pattern = None
            for pattern_name, pattern in [
                ("code", re.compile(r"`([^`]+)`")),
                ("bold", re.compile(r"\*\*(.+?)\*\*(?!\*)")),
                ("italic", re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")),
                ("link", re.compile(r"\[(.+?)\]\((.+?)\)")),
            ]:
                match = pattern.search(remaining)
                if match and (best_match is None or match.start() < best_match.start()):
                    best_match = match
                    best_pattern = pattern_name
            if best_match:
                if best_match.start() > 0:
                    tokens.append(("text", remaining[:best_match.start()], None))
                if best_pattern == "link":
                    tokens.append(("link", best_match.group(1), best_match.group(2)))
                elif best_pattern == "code":
                    tokens.append(("code", best_match.group(1), None))
                elif best_pattern == "bold":
                    tokens.append(("bold", best_match.group(1), None))
                elif best_pattern == "italic":
                    tokens.append(("italic", best_match.group(1), None))
                remaining = remaining[best_match.end():]
            else:
                tokens.append(("text", remaining, None))
                remaining = ""
        # Merge consecutive text tokens
        merged = []
        for token in tokens:
            if token[0] == "text" and merged and merged[-1][0] == "text":
                prev = merged.pop()
                merged.append(("text", prev[1] + token[1], None))
            else:
                merged.append(token)
        return merged
