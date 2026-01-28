import time
from datetime import datetime

import requests
from sqlalchemy import case
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models import Audience, Bot, BotVerification, VerificationRunStatus, VerificationStatus
from app.utils.security import decrypt_token

REQUEST_INTERVAL_SECONDS = 1 / 15
LOCALE_PRIORITY = [
    "ru",
    "uk",
    "be",
    "kk",
    "uz",
    "ky",
    "tg",
    "hy",
    "az",
    "ka",
    "ro",
    "zh-hans",
    "zh-hant",
    "ja",
    "ko",
    "id",
    "ms",
    "th",
    "vi",
    "hi",
    "bn",
    "ur",
    "en",
]


def _run_verification(db: Session, bot_id: int, locale: str | None = None) -> None:
    bot = db.query(Bot).filter_by(id=bot_id).first()
    if not bot:
        return
    verification = db.query(BotVerification).filter_by(bot_id=bot_id).first()
    if not verification:
        verification = BotVerification(bot_id=bot_id, status=VerificationRunStatus.RUNNING)
        db.add(verification)
        db.commit()
    verification.status = VerificationRunStatus.RUNNING
    db.commit()

    token = decrypt_token(bot.token_encrypted)
    last_request_time = 0.0

    while True:
        locale_order = case(
            {locale_name: idx for idx, locale_name in enumerate(LOCALE_PRIORITY)},
            value=Audience.locale,
            else_=len(LOCALE_PRIORITY) + 1,
        )
        query = db.query(Audience).filter(
            Audience.bot_id == bot_id,
            Audience.verification_status == VerificationStatus.UNKNOWN,
        )
        if locale:
            query = query.filter(Audience.locale == locale)
        batch = (
            query.order_by(locale_order.asc(), Audience.tg_id.asc())
            .limit(200)
            .all()
        )
        if not batch:
            if not locale:
                verification.status = VerificationRunStatus.COMPLETED
                verification.finished_at = datetime.utcnow()
                db.commit()
            break
        for audience in batch:
            elapsed = time.time() - last_request_time
            if elapsed < REQUEST_INTERVAL_SECONDS:
                time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)
            last_request_time = time.time()

            payload = {"chat_id": audience.tg_id, "action": "typing"}
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendChatAction",
                data=payload,
                timeout=10,
            )
            data = resp.json()
            status = VerificationStatus.OK
            if not data.get("ok"):
                description = data.get("description", "")
                if "blocked" in description.lower():
                    status = VerificationStatus.BLOCKED
                elif "chat not found" in description.lower():
                    status = VerificationStatus.NOT_STARTED
                elif "too many requests" in description.lower():
                    retry_after = data.get("parameters", {}).get("retry_after", 1)
                    time.sleep(retry_after)
                    continue
                else:
                    status = VerificationStatus.OTHER_ERROR
            audience.verification_status = status
            audience.last_verified_at = datetime.utcnow()

            verification.verified_users += 1
            if status == VerificationStatus.OK:
                verification.ok_count += 1
            elif status == VerificationStatus.BLOCKED:
                verification.blocked_count += 1
            elif status == VerificationStatus.NOT_STARTED:
                verification.not_started_count += 1
            else:
                verification.other_error_count += 1
            verification.last_processed_tg_id = audience.tg_id

            db.commit()


@celery_app.task(name="app.tasks.verification.start_verification")
def start_verification(bot_id: int) -> None:
    db: Session = SessionLocal()
    try:
        _run_verification(db, bot_id)
    finally:
        db.close()


@celery_app.task(name="app.tasks.verification.start_verification_for_locale")
def start_verification_for_locale(bot_id: int, locale: str) -> None:
    db: Session = SessionLocal()
    try:
        _run_verification(db, bot_id, locale=locale)
    finally:
        db.close()
