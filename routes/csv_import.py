"""
UgoingViral — CSV Import for Post Scheduling
POST /api/posts/import-csv  → bulk-schedule posts from CSV upload
"""
import csv, io, uuid
from datetime import datetime
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from routes.auth import get_current_user
from services.store import store, save_store

router = APIRouter()

VALID_PLATFORMS = {"instagram", "tiktok", "youtube", "twitter", "facebook"}


@router.post("/api/posts/import-csv")
async def import_csv(file: UploadFile = File(...),
                     current_user: dict = Depends(get_current_user)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    # Normalize headers (lowercase, strip whitespace)
    if reader.fieldnames is None:
        raise HTTPException(400, "CSV has no headers")

    rows = list(reader)
    if not rows:
        raise HTTPException(400, "CSV is empty")

    # Accept flexible column names
    def _col(row, *names):
        for n in names:
            for k in row:
                if k.strip().lower() == n.lower():
                    return row[k].strip()
        return ""

    imported  = []
    errors    = []
    posts     = store.get("scheduled_posts", [])
    now_iso   = datetime.utcnow().isoformat()

    for i, row in enumerate(rows, 1):
        caption    = _col(row, "caption", "content", "text")
        hashtags   = _col(row, "hashtags", "tags")
        platform   = _col(row, "platform").lower() or "instagram"
        sched_date = _col(row, "date", "scheduled_date", "scheduled_on")
        sched_time = _col(row, "time", "scheduled_time", "post_time") or "09:00"

        if not caption:
            errors.append(f"Row {i}: missing caption")
            continue
        if platform not in VALID_PLATFORMS:
            platform = "instagram"

        # Build ISO datetime
        try:
            if "T" in sched_date:
                scheduled_time = sched_date
            else:
                t = sched_time[:5] if len(sched_time) >= 5 else "09:00"
                scheduled_time = f"{sched_date}T{t}:00"
            datetime.fromisoformat(scheduled_time)  # validate
        except Exception:
            errors.append(f"Row {i}: invalid date '{sched_date}' — use YYYY-MM-DD")
            continue

        full_content = caption
        if hashtags:
            full_content = caption + "\n\n" + hashtags

        post = {
            "id":             uuid.uuid4().hex[:12],
            "content":        full_content,
            "caption":        caption,
            "hashtags":       hashtags,
            "platform":       platform,
            "scheduled_time": scheduled_time,
            "status":         "scheduled",
            "source":         "csv_import",
            "created_at":     now_iso,
        }
        posts.append(post)
        imported.append(post)

    if imported:
        store["scheduled_posts"] = posts
        save_store()

    return {
        "ok":       True,
        "imported": len(imported),
        "errors":   errors[:10],
        "posts":    imported,
    }
