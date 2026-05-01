import json
import os
import copy
from contextvars import ContextVar
from datetime import datetime

_store_ctx: ContextVar = ContextVar('store', default=None)
_uid_ctx: ContextVar = ContextVar('user_id', default=None)

USER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "user_data")
os.makedirs(USER_DATA_DIR, exist_ok=True)

PLATFORMS = ["instagram", "tiktok", "facebook", "twitter", "youtube", "snapchat", "pinterest"]

DEFAULT_STORE = {
    "settings": {
        "shopify_store": "", "shopify_token": "",
        "instagram_user": "", "instagram_pass": "",
        "tiktok_user": "", "tiktok_pass": "",
        "facebook_user": "", "facebook_pass": "",
        "twitter_user": "", "twitter_pass": "",
        "youtube_user": "", "youtube_pass": "",
        "anthropic_key": "", "openai_key": "",
        "api_enabled": {p: False for p in PLATFORMS},
    },
    "automation": {
        "active": False, "posts_per_day": 3, "videos_per_day": 1, "reels_per_day": 1,
        "hooks_per_product": 2, "captions_per_product": 2, "repeat_pattern": False,
        "no_duplicate_days": True, "post_order": "random",
        "platforms": {
            "instagram": {"active": True,  "auto_post": False, "auto_dm": False, "auto_comments": False},
            "tiktok":    {"active": False, "auto_post": False, "auto_dm": False, "auto_comments": False},
            "facebook":  {"active": False, "auto_post": False, "auto_dm": False, "auto_comments": False},
            "twitter":   {"active": False, "auto_post": False, "auto_dm": False, "auto_comments": False},
        },
        "language": "da",
        "schedule_days": [1, 2, 3, 4, 5],
        "post_times": ["09:00", "14:00", "18:00"],
    },
    "dm_settings": {
        "auto_reply_dm": False, "auto_reply_comments": False,
        "reply_tone": "venlig", "custom_instructions": "", "reply_language": "da",
        "ghost_mode": True, "platforms": ["instagram"],
        "forbidden_topics": "", "allowed_topics": "",
    },
    "scheduled_posts": [], "content_history": [],
    "product_content": {}, "automation_log": [],
    "manual_products": [], "creators": [],
    "image_rotation": {}, "lib_folders": [],
    "scheduler_log": {}, "shopify_products_cache": [],
    "email_settings": {}, "email_templates": [],
    "groups": ["Alle"], "admin_tokens": [],
    "billing": {"plan": "free", "credits": 10},
}


def _store_path(user_id: str) -> str:
    return os.path.join(USER_DATA_DIR, f"{user_id}.json")


def _load_user_store(user_id: str) -> dict:
    path = _store_path(user_id)
    if not os.path.exists(path):
        return copy.deepcopy(DEFAULT_STORE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = copy.deepcopy(DEFAULT_STORE)
        merged.update(data)
        return merged
    except Exception:
        return copy.deepcopy(DEFAULT_STORE)


def _save_user_store(user_id: str, data: dict) -> None:
    path = _store_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def get_all_user_ids() -> list:
    if not os.path.exists(USER_DATA_DIR):
        return []
    return [f[:-5] for f in os.listdir(USER_DATA_DIR) if f.endswith('.json')]


def set_user_context(user_id: str):
    user_store = _load_user_store(user_id)
    t1 = _uid_ctx.set(user_id)
    t2 = _store_ctx.set(user_store)
    return (t1, t2)


def reset_user_context(tokens):
    t1, t2 = tokens
    _uid_ctx.reset(t1)
    _store_ctx.reset(t2)


class _StoreProxy:
    def _s(self) -> dict:
        s = _store_ctx.get(None)
        return s if s is not None else {}

    def __getitem__(self, key):
        return self._s()[key]

    def __setitem__(self, key, value):
        s = _store_ctx.get(None)
        if s is not None:
            s[key] = value

    def __delitem__(self, key):
        s = _store_ctx.get(None)
        if s is not None and key in s:
            del s[key]

    def __contains__(self, key):
        return key in self._s()

    def get(self, key, default=None):
        return self._s().get(key, default)

    def setdefault(self, key, default=None):
        s = _store_ctx.get(None)
        if s is None:
            return default
        return s.setdefault(key, default)

    def update(self, data: dict):
        s = _store_ctx.get(None)
        if s is not None:
            s.update(data)

    def items(self):
        return self._s().items()

    def keys(self):
        return self._s().keys()

    def values(self):
        return self._s().values()


store = _StoreProxy()


def load_store(user_id: str = None) -> dict:
    uid = user_id or _uid_ctx.get(None)
    if uid is None:
        return copy.deepcopy(DEFAULT_STORE)
    return _load_user_store(uid)


def save_store(user_id: str = None) -> None:
    uid = user_id or _uid_ctx.get(None)
    if uid is None:
        return
    s = _store_ctx.get(None)
    if s is not None:
        _save_user_store(uid, s)


def add_log(msg: str, level: str = "info", user_id: str = None) -> None:
    uid = user_id or _uid_ctx.get(None)
    if uid is None:
        return
    s = _store_ctx.get(None)
    if s is None:
        return
    s.setdefault("automation_log", []).insert(0, {
        "time": datetime.now().strftime("%H:%M"),
        "msg": msg,
        "level": level
    })
    s["automation_log"] = s["automation_log"][:200]
    _save_user_store(uid, s)
