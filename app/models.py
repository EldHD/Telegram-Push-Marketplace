import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, BigInteger, UniqueConstraint, Enum, Boolean
from sqlalchemy.orm import relationship
from .db import Base


class VerificationStatus(str, enum.Enum):
    UNKNOWN = "UNKNOWN"
    OK = "OK"
    BLOCKED = "BLOCKED"
    NOT_STARTED = "NOT_STARTED"
    OTHER_ERROR = "OTHER_ERROR"


class VerificationRunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class BotOwner(Base):
    __tablename__ = "bot_owners"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    bots = relationship("Bot", back_populates="owner")


class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("bot_owners.id"), nullable=False)
    username = Column(String, nullable=False)
    token_encrypted = Column(String, nullable=False)
    max_pushes_per_user_per_day = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("BotOwner", back_populates="bots")
    audience = relationship("Audience", back_populates="bot")
    verification = relationship("BotVerification", back_populates="bot", uselist=False)
    pricing = relationship("BotPricing", back_populates="bot")


class Audience(Base):
    __tablename__ = "audience"

    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    tg_id = Column(BigInteger, nullable=False)
    locale = Column(String, nullable=False)
    verification_status = Column(Enum(VerificationStatus), nullable=False, default=VerificationStatus.UNKNOWN)
    last_verified_at = Column(DateTime)

    bot = relationship("Bot", back_populates="audience")

    __table_args__ = (UniqueConstraint("bot_id", "tg_id", name="uq_audience_bot_tg"),)


class BotVerification(Base):
    __tablename__ = "bot_verification"

    bot_id = Column(Integer, ForeignKey("bots.id"), primary_key=True)
    status = Column(Enum(VerificationRunStatus), nullable=False, default=VerificationRunStatus.RUNNING)
    total_users = Column(Integer, nullable=False, default=0)
    verified_users = Column(Integer, nullable=False, default=0)
    ok_count = Column(Integer, nullable=False, default=0)
    blocked_count = Column(Integer, nullable=False, default=0)
    not_started_count = Column(Integer, nullable=False, default=0)
    other_error_count = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
    last_processed_tg_id = Column(BigInteger, default=0)
    eta_seconds = Column(Integer, default=0)

    bot = relationship("Bot", back_populates="verification")


class BotPricing(Base):
    __tablename__ = "bot_pricing"

    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    locale = Column(String, nullable=False)
    cpm_cents = Column(Integer)
    is_for_sale = Column(Boolean, default=True)

    bot = relationship("Bot", back_populates="pricing")

    __table_args__ = (UniqueConstraint("bot_id", "locale", name="uq_pricing_bot_locale"),)
