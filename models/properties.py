"""Page property builders."""

from typing import Dict, Any, Optional, List


def build_title_property(title: str) -> Dict:
    return {"title": [{"type": "text", "text": {"content": title}}]}


def build_rich_text_property(text: str) -> Dict:
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def build_number_property(value: float) -> Dict:
    return {"number": value}


def build_select_property(option_name: str) -> Dict:
    return {"select": {"name": option_name}}


def build_multi_select_property(option_names: List[str]) -> Dict:
    return {"multi_select": [{"name": name} for name in option_names]}


def build_date_property(start: str, end: Optional[str] = None) -> Dict:
    date_obj = {"start": start}
    if end:
        date_obj["end"] = end
    return {"date": date_obj}


def build_checkbox_property(checked: bool) -> Dict:
    return {"checkbox": checked}


def build_url_property(url: str) -> Dict:
    return {"url": url}


def build_email_property(email: str) -> Dict:
    return {"email": email}


def build_phone_property(phone: str) -> Dict:
    return {"phone_number": phone}


def build_status_property(status_name: str) -> Dict:
    return {"status": {"name": status_name}}


def build_relation_property(page_ids: List[str]) -> Dict:
    return {"relation": [{"id": pid} for pid in page_ids]}


def build_people_property(user_ids: List[str]) -> Dict:
    return {"people": [{"object": "user", "id": uid} for uid in user_ids]}
