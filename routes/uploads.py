"""
UgoingViral — Media Upload Route
POST /api/uploads/media     Upload video/audio file
GET  /api/uploads/my        List user's uploads
DELETE /api/uploads/{fname} Delete a file
"""
import os
import uuid
import mimetypes
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Request
from fastapi.responses import JSONResponse
from routes.auth import get_current_user

router = APIRouter()

UPLOADS_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads", "user_media")
ALLOWED_TYPES = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/x-msvideo": "avi",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}
MAX_BYTES = 500 * 1024 * 1024  # 500 MB
os.makedirs(UPLOADS_BASE, exist_ok=True)


def _user_dir(user_id: str) -> str:
    d = os.path.join(UPLOADS_BASE, user_id)
    os.makedirs(d, exist_ok=True)
    return d


def _user_index_path(user_id: str) -> str:
    return os.path.join(_user_dir(user_id), "_index.json")


def _load_index(user_id: str) -> list:
    import json
    p = _user_index_path(user_id)
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return []


def _save_index(user_id: str, index: list):
    import json
    with open(_user_index_path(user_id), "w") as f:
        json.dump(index, f, indent=2)


@router.post("/api/uploads/media")
async def upload_media(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    uid = current_user["id"]
    content_type = file.content_type or ""
    ext = ALLOWED_TYPES.get(content_type)

    if not ext:
        # Try by filename extension
        fname_lower = (file.filename or "").lower()
        for allowed_ext in ["mp4", "mov", "mp3", "wav", "avi"]:
            if fname_lower.endswith(f".{allowed_ext}"):
                ext = allowed_ext
                break

    if not ext:
        raise HTTPException(
            status_code=415,
            detail="Unsupported file type. Allowed: mp4, mov, mp3, wav"
        )

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Max 500 MB.")

    file_id = str(uuid.uuid4())[:8]
    safe_name = f"{file_id}.{ext}"
    file_path = os.path.join(_user_dir(uid), safe_name)
    with open(file_path, "wb") as f:
        f.write(data)

    size_mb = round(len(data) / 1024 / 1024, 1)
    entry = {
        "id": file_id,
        "filename": safe_name,
        "original_name": file.filename or safe_name,
        "type": ext,
        "media_type": "video" if ext in ("mp4", "mov", "avi") else "audio",
        "size_mb": size_mb,
        "url": f"/uploads/user_media/{uid}/{safe_name}",
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    index = _load_index(uid)
    index.append(entry)
    _save_index(uid, index)

    return {"ok": True, "file": entry}


@router.get("/api/uploads/my")
def list_uploads(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    index = _load_index(uid)
    # Filter to files that still exist on disk
    existing = []
    for e in index:
        fp = os.path.join(_user_dir(uid), e["filename"])
        if os.path.exists(fp):
            existing.append(e)
    if len(existing) != len(index):
        _save_index(uid, existing)
    return {"files": list(reversed(existing))}


@router.delete("/api/uploads/{file_id}")
def delete_upload(file_id: str, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    index = _load_index(uid)
    entry = next((e for e in index if e["id"] == file_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="File not found")
    fp = os.path.join(_user_dir(uid), entry["filename"])
    if os.path.exists(fp):
        os.remove(fp)
    _save_index(uid, [e for e in index if e["id"] != file_id])
    return {"ok": True}
