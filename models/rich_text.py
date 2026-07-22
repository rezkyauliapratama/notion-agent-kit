"""Rich text builder utilities."""

from typing import List, Dict, Optional, Any


def build_rich_text(text: str, bold: bool = False, italic: bool = False,
                    code: bool = False, underline: bool = False,
                    strikethrough: bool = False, link_url: Optional[str] = None,
                    color: str = "default") -> List[Dict]:
    annotations = {"bold": bold, "italic": italic, "strikethrough": strikethrough,
                   "underline": underline, "code": code, "color": color}
    rt = {"type": "text", "text": {"content": text}, "annotations": annotations}
    if link_url:
        rt["text"]["link"] = {"url": link_url}
    return [rt]


def build_mention(user_id: str) -> List[Dict]:
    return [{"type": "mention", "mention": {"type": "user", "user": {"object": "user", "id": user_id}},
             "annotations": {"bold": False, "italic": False, "strikethrough": False,
                             "underline": False, "code": False, "color": "default"}}]


def build_date_mention(date_str: str) -> List[Dict]:
    return [{"type": "mention", "mention": {"type": "date", "date": {"start": date_str}},
             "annotations": {"bold": False, "italic": False, "strikethrough": False,
                             "underline": False, "code": False, "color": "default"}}]


def build_mixed_rich_text(segments: List[Dict[str, Any]]) -> List[Dict]:
    result = []
    for seg in segments:
        rt = {"type": "text", "text": {"content": seg.get("text", "")},
              "annotations": {"bold": seg.get("bold", False), "italic": seg.get("italic", False),
                              "strikethrough": seg.get("strikethrough", False),
                              "underline": seg.get("underline", False),
                              "code": seg.get("code", False), "color": seg.get("color", "default")}}
        if seg.get("link_url"):
            rt["text"]["link"] = {"url": seg["link_url"]}
        result.append(rt)
    return result
