"""Fire-and-forget helpers — never block the JSON path."""
from datetime import datetime


def pg_sync_user_store(user_id: str, data: dict):
    try:
        from services.db import get_db_session, DbUserStore
        with get_db_session() as s:
            if s is None:
                return
            row = s.get(DbUserStore, user_id)
            if row:
                row.data = data
                row.updated_at = datetime.utcnow()
            else:
                s.add(DbUserStore(user_id=user_id, data=data))
    except Exception:
        pass


def pg_create_user(user: dict):
    try:
        from services.db import get_db_session, DbUser
        with get_db_session() as s:
            if s is None:
                return
            if s.get(DbUser, user.get("id")):
                return
            s.add(DbUser(
                id=user.get("id", ""),
                email=user.get("email", ""),
                name=user.get("name", ""),
                company=user.get("company", ""),
                niche=user.get("niche", ""),
                plan=user.get("plan", "free"),
                credits=user.get("credits", 100),
            ))
    except Exception:
        pass


def pg_update_user_billing(user_id: str, plan: str, credits: int):
    try:
        from services.db import get_db_session, DbUser
        with get_db_session() as s:
            if s is None:
                return
            row = s.get(DbUser, user_id)
            if row:
                row.plan = plan
                row.credits = credits
                row.updated_at = datetime.utcnow()
    except Exception:
        pass


def pg_add_billing_event(user_id: str, event_type: str, credits: int = 0,
                          plan: str = "", note: str = ""):
    try:
        from services.db import get_db_session, DbBillingEvent
        with get_db_session() as s:
            if s is None:
                return
            s.add(DbBillingEvent(
                user_id=user_id,
                event_type=event_type,
                credits=credits,
                plan=plan,
                note=note[:500],
            ))
    except Exception:
        pass


def pg_upsert_workspace(user_id: str, ws: dict, is_active: bool = False):
    try:
        from services.db import get_db_session, DbWorkspace
        with get_db_session() as s:
            if s is None:
                return
            row = s.get(DbWorkspace, ws.get("id", ""))
            if row:
                row.name = ws.get("name", row.name)
                row.niche = ws.get("niche", row.niche)
                row.is_active = is_active
            else:
                s.add(DbWorkspace(
                    id=ws.get("id", ""),
                    user_id=user_id,
                    name=ws.get("name", "My Workspace"),
                    niche=ws.get("niche", ""),
                    is_active=is_active,
                ))
    except Exception:
        pass
