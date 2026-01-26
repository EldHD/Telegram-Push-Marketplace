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
        conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_bots_username ON bots (username)")
        )
