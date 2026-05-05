"""UgoingViral — credit costs per action."""

VIDEO_GENERATION = {
    "text_to_video_10s": 250,
    "image_to_video_10s": 250,
    "extended_per_10s": 50,  # added per extra 10 sec
}

VIDEO_EDITING = {
    "upload_and_edit": 50,
    "effects_filter": 25,
    "upscale_4k": 30,
}

VOICE_OVER = {
    "per_30s": 10,
}

AUTOPILOT = {
    "minimum_credits_to_activate": 200,
    "pause_threshold": 50,  # pause and alert when below this
    "per_day_1_platform": 20,
    "per_day_3_platforms": 50,
    "per_day_5_plus_platforms": 80,
}

AUTO_ENGAGEMENT = {
    "per_100_dms": 15,
    "per_100_comments": 10,
}

STUDIO_PACKS = {
    "scenes_3": {"price_usd": 29, "scenes": 3, "secs_per_scene": 10},
    "scenes_6": {"price_usd": 59, "scenes": 6, "secs_per_scene": 10},
    "scenes_10": {"price_usd": 99, "scenes": 10, "secs_per_scene": 10},
}
