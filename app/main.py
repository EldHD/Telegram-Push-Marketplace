import csv
import io
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .db import Base, engine, get_db
from .models import (
    Audience,
    Bot,
    BotOwner,
    BotPricing,
    BotVerification,
    VerificationRunStatus,
    VerificationStatus,
)
from .tasks.verification import start_verification
from .utils.locale import is_valid_locale, normalize_locale
from .utils.security import encrypt_token

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-secret"))

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URL = os.getenv("OAUTH_REDIRECT_URL", "http://localhost:8000/auth/callback")

TEST_PUSH_RATE_LIMIT_SECONDS = 5


oauth = OAuth()
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def require_login(request: Request) -> str:
    email = request.session.get("user")
    if not email:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return email


def gmail_only(email: str) -> bool:
    return email.lower().endswith("@gmail.com")


def get_owner(db: Session, email: str) -> BotOwner:
    owner = db.query(BotOwner).filter_by(email=email).first()
    if not owner:
        owner = BotOwner(email=email)
        db.add(owner)
        db.commit()
        db.refresh(owner)
    return owner


def validate_bot_username(username: str) -> bool:
    return bool(re.match(r"^@[a-z0-9_]{5,64}bot$", username.lower()))


def telegram_get_me(token: str) -> Dict:
    resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    data = resp.json()
    if not data.get("ok"):
        raise ValueError(data.get("description", "Unable to validate bot token"))
    return data


def build_error_report(errors: List[Tuple[int, str, str, str]]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["row", "tg_id", "locale", "error"])
    for row_number, tg_id, locale, reason in errors:
        writer.writerow([row_number, tg_id, locale, reason])
    return output.getvalue().encode()


def compute_locale_summary(rows: List[Tuple[str, int, int, int]]):
    primary = []
    other = {"total": 0, "ok": 0, "not_started": 0, "blocked": 0}
    for locale, total, ok, not_started, blocked in rows:
        if total >= 1000:
            primary.append({
                "locale": locale,
                "total": total,
                "ok": ok,
                "not_started": not_started,
                "blocked": blocked,
            })
        else:
            other["total"] += total
            other["ok"] += ok
            other["not_started"] += not_started
            other["blocked"] += blocked
    return primary, other


def allowed_html(value: str) -> bool:
    allowed_tags = {"b", "i", "u", "s", "a", "code", "pre"}
    tags = re.findall(r"</?([a-zA-Z0-9]+)", value)
    return all(tag in allowed_tags for tag in tags)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/bot-owner")
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "google_enabled": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
        },
    )


@app.get("/auth/google")
async def auth_google(request: Request):
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")
    redirect_uri = OAUTH_REDIRECT_URL
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user = token.get("userinfo")
    if not user:
        raise HTTPException(status_code=400, detail="Unable to fetch user info")
    email = user.get("email", "")
    if not gmail_only(email):
        raise HTTPException(status_code=403, detail="Only Gmail accounts are allowed")
    get_owner(db, email)
    request.session["user"] = email
    return RedirectResponse("/bot-owner")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/bot-owner", response_class=HTMLResponse)
async def bot_owner_portal(request: Request, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bots = db.query(Bot).filter_by(owner_id=owner.id).order_by(Bot.created_at.desc()).all()
    selected_bot_id = request.query_params.get("bot_id")
    selected_bot = None
    if selected_bot_id:
        selected_bot = db.query(Bot).filter_by(id=int(selected_bot_id), owner_id=owner.id).first()
    elif bots:
        selected_bot = bots[0]
    verification = None
    pricing_rows = []
    locale_summary = []
    other_locales = None
    if selected_bot:
        verification = db.query(BotVerification).filter_by(bot_id=selected_bot.id).first()
        locale_summary = db.execute(
            """
            SELECT locale,
                   COUNT(*) as total,
                   SUM(CASE WHEN verification_status = 'OK' THEN 1 ELSE 0 END) as ok,
                   SUM(CASE WHEN verification_status = 'NOT_STARTED' THEN 1 ELSE 0 END) as not_started,
                   SUM(CASE WHEN verification_status = 'BLOCKED' THEN 1 ELSE 0 END) as blocked
            FROM audience
            WHERE bot_id = :bot_id
            GROUP BY locale
            """,
            {"bot_id": selected_bot.id},
        ).fetchall()
        locale_summary, other_locales = compute_locale_summary(locale_summary)
        pricing_rows = db.query(BotPricing).filter_by(bot_id=selected_bot.id).all()
    return templates.TemplateResponse(
        "bot_owner.html",
        {
            "request": request,
            "owner": owner,
            "bots": bots,
            "selected_bot": selected_bot,
            "verification": verification,
            "locale_summary": locale_summary,
            "other_locales": other_locales,
            "pricing_rows": pricing_rows,
        },
    )


@app.post("/bot-owner/bots")
async def create_bot(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    token: str = Form(...),
    max_pushes_per_user_per_day: int = Form(1),
):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    errors = {}
    if not validate_bot_username(username):
        errors["username"] = "Bot username must look like @mybot and end with 'bot'."
    if max_pushes_per_user_per_day < 1:
        errors["max_pushes_per_user_per_day"] = "Max pushes per user must be at least 1."
    if errors:
        return templates.TemplateResponse(
            "bot_owner.html",
            {
                "request": request,
                "owner": owner,
                "bots": db.query(Bot).filter_by(owner_id=owner.id).all(),
                "selected_bot": None,
                "errors": errors,
            },
            status_code=400,
        )
    try:
        data = telegram_get_me(token)
    except ValueError as exc:
        errors["token"] = str(exc)
        return templates.TemplateResponse(
            "bot_owner.html",
            {
                "request": request,
                "owner": owner,
                "bots": db.query(Bot).filter_by(owner_id=owner.id).all(),
                "selected_bot": None,
                "errors": errors,
            },
            status_code=400,
        )
    api_username = data.get("result", {}).get("username", "")
    if api_username.lower() != username.lstrip("@").lower():
        errors["username"] = "Provided username does not match Telegram's getMe response."
    if errors:
        return templates.TemplateResponse(
            "bot_owner.html",
            {
                "request": request,
                "owner": owner,
                "bots": db.query(Bot).filter_by(owner_id=owner.id).all(),
                "selected_bot": None,
                "errors": errors,
            },
            status_code=400,
        )
    encrypted_token = encrypt_token(token)
    bot = Bot(
        owner_id=owner.id,
        username=username.lower(),
        token_encrypted=encrypted_token,
        max_pushes_per_user_per_day=max_pushes_per_user_per_day,
    )
    db.add(bot)
    db.commit()
    return RedirectResponse(f"/bot-owner?bot_id={bot.id}", status_code=303)


@app.post("/bot-owner/bots/{bot_id}/upload")
async def upload_audience(
    request: Request,
    bot_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = db.query(Bot).filter_by(id=bot_id, owner_id=owner.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode()))
    errors = []
    accepted = 0
    total = 0
    for row_number, row in enumerate(reader, start=2):
        total += 1
        tg_id_raw = row.get("tg_id", "").strip()
        locale_raw = row.get("locale", "").strip()
        reason = None
        if not tg_id_raw.isdigit() or int(tg_id_raw) <= 0:
            reason = "tg_id must be a positive integer"
        normalized_locale = normalize_locale(locale_raw)
        if not normalized_locale or not is_valid_locale(normalized_locale):
            reason = "locale must be in format xx or xx-YY"
        if reason:
            errors.append((row_number, tg_id_raw, locale_raw, reason))
            continue
        exists = db.query(Audience).filter_by(bot_id=bot_id, tg_id=int(tg_id_raw)).first()
        if exists:
            errors.append((row_number, tg_id_raw, locale_raw, "Duplicate tg_id for this bot"))
            continue
        audience = Audience(bot_id=bot_id, tg_id=int(tg_id_raw), locale=normalized_locale)
        db.add(audience)
        accepted += 1
    db.commit()

    error_report_id = None
    if errors:
        data = build_error_report(errors)
        error_report_id = str(uuid.uuid4())
        Path("app/data").mkdir(parents=True, exist_ok=True)
        Path(f"app/data/{error_report_id}.csv").write_bytes(data)

    if accepted:
        total_users = db.query(Audience).filter_by(bot_id=bot_id).count()
        eta_seconds = int(total_users / 15)
        verification = db.query(BotVerification).filter_by(bot_id=bot_id).first()
        if not verification:
            verification = BotVerification(
                bot_id=bot_id,
                status=VerificationRunStatus.RUNNING,
                total_users=total_users,
                eta_seconds=eta_seconds,
            )
            db.add(verification)
        else:
            verification.status = VerificationRunStatus.RUNNING
            verification.total_users = total_users
            verification.eta_seconds = eta_seconds
        db.commit()
        start_verification.delay(bot_id)

    params = f"bot_id={bot_id}&uploaded=1"
    if error_report_id:
        params += f"&error_report_id={error_report_id}"
    params += f"&total={total}&accepted={accepted}&rejected={len(errors)}"
    return RedirectResponse(f"/bot-owner?{params}", status_code=303)


@app.get("/bot-owner/bots/{bot_id}/error-report")
async def download_error_report(request: Request, bot_id: int, report_id: str):
    require_login(request)
    path = Path(f"app/data/{report_id}.csv")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return StreamingResponse(
        io.BytesIO(path.read_bytes()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audience-errors-{bot_id}.csv"},
    )


@app.get("/bot-owner/bots/{bot_id}/verification/status")
async def verification_status(request: Request, bot_id: int, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = db.query(Bot).filter_by(id=bot_id, owner_id=owner.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    verification = db.query(BotVerification).filter_by(bot_id=bot_id).first()
    if not verification:
        return {"status": "NONE"}
    remaining = max(verification.total_users - verification.verified_users, 0)
    eta_seconds = int(remaining / 15)
    return {
        "status": verification.status,
        "total": verification.total_users,
        "verified": verification.verified_users,
        "ok": verification.ok_count,
        "blocked": verification.blocked_count,
        "not_started": verification.not_started_count,
        "other_error": verification.other_error_count,
        "eta_seconds": eta_seconds,
    }


@app.post("/bot-owner/bots/{bot_id}/pricing")
async def save_pricing(request: Request, bot_id: int, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = db.query(Bot).filter_by(id=bot_id, owner_id=owner.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    form = await request.form()
    locale_inputs = [key for key in form.keys() if key.startswith("locale_")]
    enabled_locales = 0
    for locale_key in locale_inputs:
        locale = locale_key.replace("locale_", "")
        is_for_sale = form.get(f"sale_{locale}") == "on"
        cpm_raw = form.get(f"cpm_{locale}")
        if is_for_sale:
            enabled_locales += 1
            if not cpm_raw or int(cpm_raw) <= 0:
                raise HTTPException(status_code=400, detail="CPM must be positive")
        pricing = db.query(BotPricing).filter_by(bot_id=bot_id, locale=locale).first()
        if not pricing:
            pricing = BotPricing(bot_id=bot_id, locale=locale)
            db.add(pricing)
        pricing.is_for_sale = is_for_sale
        pricing.cpm_cents = int(cpm_raw) if is_for_sale else None
    if enabled_locales == 0:
        raise HTTPException(status_code=400, detail="At least one locale must be for sale")
    db.commit()
    return RedirectResponse(f"/bot-owner?bot_id={bot_id}&pricing_saved=1", status_code=303)


@app.post("/bot-owner/bots/{bot_id}/test-push")
async def test_push(
    request: Request,
    bot_id: int,
    db: Session = Depends(get_db),
    tg_id: int = Form(...),
    message: str = Form(...),
):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = db.query(Bot).filter_by(id=bot_id, owner_id=owner.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if not allowed_html(message):
        raise HTTPException(status_code=400, detail="Message contains unsupported HTML tags")
    last_sent = request.session.get("last_test_push")
    now = time.time()
    if last_sent and now - last_sent < TEST_PUSH_RATE_LIMIT_SECONDS:
        raise HTTPException(status_code=429, detail="Please wait before sending another test")
    request.session["last_test_push"] = now
    audience = (
        db.query(Audience)
        .filter_by(bot_id=bot_id, tg_id=tg_id, verification_status=VerificationStatus.OK)
        .first()
    )
    if not audience:
        raise HTTPException(status_code=400, detail="tg_id is not verified for this bot")
    from .utils.security import decrypt_token

    token = decrypt_token(bot.token_encrypted)
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": tg_id, "text": message, "parse_mode": "HTML"},
        timeout=10,
    )
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(status_code=400, detail=data.get("description", "Send failed"))
    return RedirectResponse(f"/bot-owner?bot_id={bot_id}&test_sent=1", status_code=303)
