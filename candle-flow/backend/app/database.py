from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - 依赖 sqlite3 驱动
        """SQLite 并发读调参。

        `journal_mode` 默认 delete：写事务（盘后因子重建、K线同步，均 8 并发）
        持锁期间**读者会被阻塞**，busy_timeout 5s 一过即报 `database is locked`。
        表现为「榜单叠加技术面」在批跑期间成片返回「分析失败」。
        WAL 让读不再等待单个写者；busy_timeout 提到 15s 消化瞬时写竞争。
        该设置持久化在库文件头，设置一次即生效（-wal/-shm 为伴生文件）。
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()


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
    if "stock_info" in insp.get_table_names():
        scols = {c["name"] for c in insp.get_columns("stock_info")}
        if "pinyin" not in scols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE stock_info ADD COLUMN pinyin VARCHAR(64) NOT NULL DEFAULT ''"))
            with engine.begin() as conn:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_stock_info_pinyin ON stock_info (pinyin)"))
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
    if "invalidation_price" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE trading_signals ADD COLUMN invalidation_price NUMERIC(10, 4)"))
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
