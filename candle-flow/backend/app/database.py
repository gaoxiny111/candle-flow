from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()


def _migrate_sqlite():
    if not settings.database_url.startswith("sqlite"):
        return
    insp = inspect(engine)
    if "trading_signals" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("trading_signals")}
    if "pattern_id" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE trading_signals ADD COLUMN pattern_id INTEGER"))
    if "pattern_date" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE trading_signals ADD COLUMN pattern_date DATE"))
    if "confluence_count" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE trading_signals ADD COLUMN confluence_count INTEGER"))
    if "confluence_hits" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE trading_signals ADD COLUMN confluence_hits VARCHAR(200)"))
    if "confluence_detail" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE trading_signals ADD COLUMN confluence_detail TEXT"))
    if "user_config" in insp.get_table_names():
        ucols = {c["name"] for c in insp.get_columns("user_config")}
        if "password_hash" not in ucols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE user_config ADD COLUMN password_hash VARCHAR(128)"))
        if "watchlist" not in ucols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE user_config ADD COLUMN watchlist TEXT"))
        if "auth_token" not in ucols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE user_config ADD COLUMN auth_token VARCHAR(64)"))
        if "membership_plan" not in ucols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE user_config ADD COLUMN membership_plan VARCHAR(20) DEFAULT 'free'"))
        if "membership_expires_at" not in ucols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE user_config ADD COLUMN membership_expires_at DATETIME"))
