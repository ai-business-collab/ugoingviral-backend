"""
Per-user storage quotas for UgoingViral.

User media (videos/images/audio) lives on DISK — never in the database — under:
  - user_content/{user_id}/         (Content Library uploads + generated media)
  - uploads/user_media/{user_id}/   (legacy media-upload route)

A user may keep an UNLIMITED number of files. The only limits are:
  - per-file size  (500 MB, enforced in the upload handlers), and
  - a per-plan total storage quota (GB), enforced here.
"""
import os

_BASE = os.path.dirname(os.path.abspath(__file__))
USER_CONTENT_DIR = os.path.join(_BASE, "..", "user_content")
USER_MEDIA_DIR   = os.path.join(_BASE, "..", "uploads", "user_media")

GB = 1024 * 1024 * 1024

# Storage quota per plan, in GB. Free is intentionally small; paid tiers scale up.
# (The product markets the three public tiers as Free / Pro / Business — the
# internal plan keys map onto generous limits below.)
PLAN_STORAGE_GB = {
    "free":     1,
    "starter":  5,
    "growth":   15,
    "basic":    25,
    "pro":      50,
    "elite":    100,
    "personal": 250,
    "agency":   250,
}
DEFAULT_STORAGE_GB = 1


def quota_bytes(plan: str) -> int:
    return PLAN_STORAGE_GB.get((plan or "free").lower(), DEFAULT_STORAGE_GB) * GB


def _dir_bytes(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def user_storage_bytes(user_id: str) -> int:
    """Total bytes this user occupies on disk across all media locations."""
    if not user_id:
        return 0
    return (_dir_bytes(os.path.join(USER_CONTENT_DIR, user_id))
            + _dir_bytes(os.path.join(USER_MEDIA_DIR, user_id)))


def usage_summary(user_id: str, plan: str) -> dict:
    used = user_storage_bytes(user_id)
    quota = quota_bytes(plan)
    pct = round(used / quota * 100, 1) if quota else 0.0
    return {
        "plan": (plan or "free").lower(),
        "used_bytes": used,
        "used_mb": round(used / 1024 / 1024, 1),
        "used_gb": round(used / GB, 3),
        "quota_gb": round(quota / GB, 2),
        "quota_bytes": quota,
        "used_pct": pct,
        "remaining_gb": round(max(0, quota - used) / GB, 3),
        "over_quota": used >= quota,
    }


def remaining_bytes(user_id: str, plan: str) -> int:
    """Bytes the user may still upload before hitting their plan quota."""
    return max(0, quota_bytes(plan) - user_storage_bytes(user_id))
