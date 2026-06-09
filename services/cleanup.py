"""
Daily auto-cleanup of TEMPORARY data only — runs at 03:00 UTC.

What it removes (all transient):
  • failed video-render jobs older than 24h (in-memory queue)
  • completed render job records older than 7 days — these just cache the
    provider's temporary CDN URL, not the actual file

What it NEVER touches:
  • user uploaded files in user_content/
  • content-library items
  • generated videos saved to disk / persisted generated_videos records
  • anything the user paid credits for
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

_LOG = logging.getLogger("ugoingviral.cleanup")

CLEANUP_HOUR_UTC = 3


def _seconds_until(hour_utc: int) -> float:
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def run_cleanup_once() -> dict:
    """Execute one cleanup pass. Safe to call manually. Returns a summary."""
    from routes.studio import cleanup_temporary_data
    return cleanup_temporary_data(failed_max_age_h=24, url_cache_max_age_days=7)


async def run_daily_cleanup():
    """Background task: sleep until 03:00 UTC, run cleanup, repeat every 24h."""
    await asyncio.sleep(30)  # let the app finish starting up
    while True:
        await asyncio.sleep(_seconds_until(CLEANUP_HOUR_UTC))
        try:
            result = run_cleanup_once()
            _LOG.info("daily cleanup at 03:00 UTC: %s", result)
        except Exception as exc:
            _LOG.error("daily cleanup failed: %s", exc)
        # Avoid re-firing within the same minute before the clock advances.
        await asyncio.sleep(60)
