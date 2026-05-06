import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, Boolean,
    DateTime, Text, JSON, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base, sessionmaker
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "")

_engine = None
_SessionLocal = None
Base = declarative_base()


class DbUserStore(Base):
    """Denormalized JSONB blob — full user store, dual-write target."""
    __tablename__ = "user_store"
    user_id  = Column(String(64), primary_key=True)
    data     = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DbUser(Base):
    __tablename__ = "users"
    id         = Column(String(64), primary_key=True)
    email      = Column(String(255), nullable=False, unique=True)
    name       = Column(String(255), default="")
    company    = Column(String(255), default="")
    niche      = Column(String(128), default="")
    plan       = Column(String(32), default="free")
    credits    = Column(Integer, default=100)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DbWorkspace(Base):
    __tablename__ = "workspaces"
    id         = Column(String(64), primary_key=True)
    user_id    = Column(String(64), nullable=False, index=True)
    name       = Column(String(120), default="My Workspace")
    niche      = Column(String(128), default="")
    is_active  = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class DbBillingEvent(Base):
    __tablename__ = "billing_events"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(String(64), nullable=False, index=True)
    event_type = Column(String(64))
    credits    = Column(Integer, default=0)
    plan       = Column(String(32), default="free")
    note       = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    global _engine, _SessionLocal
    if not DATABASE_URL:
        return
    try:
        _engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            connect_args={"connect_timeout": 5},
        )
        _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
        Base.metadata.create_all(bind=_engine)
    except Exception as e:
        print(f"[db] init_db failed: {e}")
        _engine = None
        _SessionLocal = None


@contextmanager
def get_db_session():
    if _SessionLocal is None:
        yield None
        return
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
