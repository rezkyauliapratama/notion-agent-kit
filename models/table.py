"""Table builder utilities."""

from typing import List, Dict


def build_table(headers: List[str], rows: List[List[str]], has_column_header: bool = True) -> Dict:
    if not headers:
        raise ValueError("Table must have at least one column")
    num_columns = len(headers)
    children = []
    children.append(_build_table_row(headers, bold=True, num_columns=num_columns))
    for row in rows:
        padded = list(row[:num_columns])
        while len(padded) < num_columns:
            padded.append("")
        children.append(_build_table_row(padded, bold=False, num_columns=num_columns))
    return {"type": "table", "table": {"table_width": num_columns, "has_column_header": has_column_header, "children": children}}


def _build_table_row(cells: List[str], bold: bool, num_columns: int) -> Dict:
    notion_cells = []
    for cell in cells:
        notion_cells.append([{"type": "text", "text": {"content": cell},
                              "annotations": {"bold": bold, "italic": False, "strikethrough": False,
                                              "underline": False, "code": False, "color": "default"}}])
    return {"type": "table_row", "table_row": {"cells": notion_cells}}


def calculate_table_width(markdown_table_line: str) -> int:
    cells = [c.strip() for c in markdown_table_line.split("|")]
    cells = [c for c in cells if c]
    return len(cells)
