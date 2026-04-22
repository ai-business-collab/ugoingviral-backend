"""
Alle Pydantic request/response models
"""
from pydantic import BaseModel
from typing import Optional, List


class Settings(BaseModel):
    shopify_store: Optional[str] = None; shopify_token: Optional[str] = None
    instagram_user: Optional[str] = None; instagram_pass: Optional[str] = None
    tiktok_user: Optional[str] = None; tiktok_pass: Optional[str] = None
    facebook_user: Optional[str] = None; facebook_pass: Optional[str] = None
    twitter_user: Optional[str] = None; twitter_pass: Optional[str] = None
    youtube_user: Optional[str] = None; youtube_pass: Optional[str] = None
    anthropic_key: Optional[str] = None; openai_key: Optional[str] = None

class ApiToggle(BaseModel):
    platform: str; enabled: bool

class PlatformAutomation(BaseModel):
    platform: str
    active: Optional[bool] = None
    auto_post: Optional[bool] = None
    auto_dm: Optional[bool] = None
    auto_comments: Optional[bool] = None

class ContentRequest(BaseModel):
    product_id: Optional[str] = None; product_title: Optional[str] = None
    product_description: Optional[str] = None; content_type: str
    platform: str; language: str = "da"; tone: str = "engaging"
    creator_id: Optional[str] = None

class PostRequest(BaseModel):
    platform: str; content: str; image_url: Optional[str] = None
    scheduled_time: Optional[str] = None; product_id: Optional[str] = None
    mode: Optional[str] = "ui"; creator_id: Optional[str] = None

class DMReplyRequest(BaseModel):
    platform: str; message: str
    sender_name: Optional[str] = ""; context: Optional[str] = ""

class AutomationSettings(BaseModel):
    active: Optional[bool] = None; posts_per_day: Optional[int] = None
    videos_per_day: Optional[int] = None; reels_per_day: Optional[int] = None
    hooks_per_product: Optional[int] = None; captions_per_product: Optional[int] = None
    repeat_pattern: Optional[bool] = None; no_duplicate_days: Optional[bool] = None
    post_order: Optional[str] = None; language: Optional[str] = None
    schedule_days: Optional[List[int]] = None; post_times: Optional[List[str]] = None
    respect_quiet_hours: Optional[bool] = None
    quiet_start: Optional[str] = None
    quiet_end: Optional[str] = None
    max_posts_per_hour: Optional[int] = None

class DMSettings(BaseModel):
    auto_reply_dm: Optional[bool] = None; auto_reply_comments: Optional[bool] = None
    reply_tone: Optional[str] = None; custom_instructions: Optional[str] = None
    reply_language: Optional[str] = None; ghost_mode: Optional[bool] = None
    platforms: Optional[List[str]] = None
    forbidden_topics: Optional[str] = None; allowed_topics: Optional[str] = None

class ManualProduct(BaseModel):
    id: Optional[str] = None; title: str; description: Optional[str] = ""
    price: Optional[str] = "0"; images: Optional[List[str]] = []
    group: Optional[str] = "Alle"

class Creator(BaseModel):
    id: Optional[str] = None; name: str
    instagram: Optional[str] = ""; tiktok: Optional[str] = ""
    facebook: Optional[str] = ""; twitter: Optional[str] = ""
    bio: Optional[str] = ""
    product_groups: Optional[List[str]] = []
    images: Optional[List[str]] = []
    active: Optional[bool] = True

class PlaywrightPost(BaseModel):
    platform: str; content: str; image_url: Optional[str] = None
    creator_id: Optional[str] = None
