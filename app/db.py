import logging
import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@db:5432/telegram_push")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
logger = logging.getLogger(__name__)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_bot_owner_email_column() -> None:
    inspector = inspect(engine)
    if "bot_owners" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("bot_owners")}
    with engine.begin() as conn:
        if "email" not in columns:
            conn.execute(text("ALTER TABLE bot_owners ADD COLUMN IF NOT EXISTS email VARCHAR"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_bot_owners_email ON bot_owners (email)"))
        duplicate_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "SELECT email, COUNT(*) FROM bot_owners "
                "WHERE email IS NOT NULL "
                "GROUP BY email HAVING COUNT(*) > 1"
                ") duplicates"
            )
        ).scalar()
        if duplicate_count == 0:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_owners_email "
                    "ON bot_owners (email)"
                )
            )
        else:
            logger.warning("Skipped unique index on bot_owners.email due to duplicates.")


def ensure_bot_username_unique_index() -> None:
    inspector = inspect(engine)
    if "bots" not in inspector.get_table_names():
        return
    with engine.begin() as conn:
        duplicate_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "SELECT username, COUNT(*) FROM bots "
                "WHERE username IS NOT NULL "
                "GROUP BY username HAVING COUNT(*) > 1"
                ") duplicates"
            )
        ).scalar()
        if duplicate_count == 0:
            conn.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS uq_bots_username ON bots (username)")
            )
        else:
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_bots_username ON bots (username)")
            )
            logger.warning("Skipped unique index on bots.username due to duplicates.")


def ensure_bot_columns() -> None:
    inspector = inspect(engine)
    if "bots" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("bots")}
    with engine.begin() as conn:
        if "audience_total" not in columns:
            conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS audience_total INTEGER DEFAULT 0"))
        if "audience_ru" not in columns:
            conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS audience_ru INTEGER DEFAULT 0"))
        if "earned_all_time" not in columns:
            conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS earned_all_time INTEGER DEFAULT 0"))
        if "token_needs_update" not in columns:
            conn.execute(
                text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS token_needs_update BOOLEAN DEFAULT FALSE")
            )
        if "deleted_at" not in columns:
            conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP"))
        if "updated_at" not in columns:
            conn.execute(text("ALTER TABLE bots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"))
