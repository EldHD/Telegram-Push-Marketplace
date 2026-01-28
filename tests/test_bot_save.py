import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/telegram_push_test.db")

from app.db import Base, SessionLocal, engine
from app.main import apply_bot_save
from app.models import BotOwner


Base.metadata.create_all(bind=engine)


def create_owner(email: str) -> BotOwner:
    db = SessionLocal()
    owner = BotOwner(email=email)
    db.add(owner)
    db.commit()
    db.refresh(owner)
    db.close()
    return owner


def test_save_new_bot():
    db = SessionLocal()
    owner = create_owner("one@gmail.com")
    status, bot = apply_bot_save(db, owner, "@mybot", "enc-token", 1)
    assert status == "created"
    assert bot.owner_id == owner.id
    db.close()


def test_save_duplicate_same_owner():
    db = SessionLocal()
    owner = create_owner("two@gmail.com")
    apply_bot_save(db, owner, "@dupbot", "enc-token", 1)
    status, _ = apply_bot_save(db, owner, "@dupbot", "enc-token", 1)
    assert status == "duplicate"
    db.close()


def test_save_duplicate_transfers_owner():
    db = SessionLocal()
    owner_one = create_owner("three@gmail.com")
    owner_two = create_owner("four@gmail.com")
    apply_bot_save(db, owner_one, "@transferbot", "enc-token", 1)
    status, bot = apply_bot_save(db, owner_two, "@transferbot", "enc-token-2", 2)
    assert status == "transferred"
    assert bot.owner_id == owner_two.id
    assert bot.max_pushes_per_user_per_day == 2
    db.close()
