import csv
import io
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .db import (
    Base,
    engine,
    ensure_bot_columns,
    ensure_bot_owner_email_column,
    ensure_bot_username_unique_index,
    get_db,
)
from .models import (
    Audience,
    Bot,
    BotOwner,
    BotPricing,
    BotVerification,
    VerificationRunStatus,
    VerificationStatus,
)
from .tasks.verification import start_verification, start_verification_for_locale
from .utils.locale import is_valid_locale, normalize_locale
from .utils.security import decrypt_token, encrypt_token, ensure_fernet_key_config

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-secret"))


@app.on_event("startup")
def ensure_schema_on_startup() -> None:
    ensure_bot_owner_email_column()
    ensure_bot_username_unique_index()
    ensure_bot_columns()
    ensure_fernet_key_config()

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URL = os.getenv("OAUTH_REDIRECT_URL", "http://localhost:8000/auth/callback")

TEST_PUSH_RATE_LIMIT_SECONDS = 5
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{20,}$")


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


def apply_bot_save(
    db: Session,
    owner: BotOwner,
    normalized_username: str,
    encrypted_token: str,
    max_pushes_per_user_per_day: int,
):
    existing = db.query(Bot).filter_by(username=normalized_username).first()
    if existing:
        if existing.owner_id == owner.id:
            return "duplicate", existing
        existing.owner_id = owner.id
        existing.token_encrypted = encrypted_token
        existing.max_pushes_per_user_per_day = max_pushes_per_user_per_day
        existing.token_needs_update = False
        db.commit()
        return "transferred", existing
    bot = Bot(
        owner_id=owner.id,
        username=normalized_username,
        token_encrypted=encrypted_token,
        max_pushes_per_user_per_day=max_pushes_per_user_per_day,
        audience_total=0,
        audience_ru=0,
        earned_all_time=0,
        token_needs_update=False,
    )
    db.add(bot)
    db.commit()
    return "created", bot


def validate_bot_username(username: str) -> bool:
    return bool(re.match(r"^@?[a-z0-9_]{5,64}bot$", username.lower()))


def _normalize_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


def _safe_csv_reader(content: bytes) -> csv.reader:
    decoded = content.decode("utf-8-sig", errors="replace")
    sample = decoded[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t"])
    except csv.Error:
        dialect = csv.excel
    return csv.reader(io.StringIO(decoded), dialect)


def parse_audience_rows(content: bytes) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str, str, str]]]:
    reader = _safe_csv_reader(content)
    rows = list(reader)
    errors = []
    accepted = []
    if not rows:
        return accepted, errors
    first = rows[0]
    has_header = any(cell.lower().strip() in {"tg_id", "locale"} for cell in first)
    start_index = 1 if has_header else 0
    for index, row in enumerate(rows[start_index:], start=start_index + 1):
        if not row or len(row) < 2:
            errors.append((index + 1, "", "", "Row must contain tg_id and locale"))
            continue
        tg_id_raw = str(row[0]).strip()
        locale_raw = str(row[1]).strip()
        if not tg_id_raw.isdigit() or int(tg_id_raw) <= 0:
            errors.append((index + 1, tg_id_raw, locale_raw, "tg_id must be a positive integer"))
            continue
        normalized_locale = normalize_locale(locale_raw.replace("_", "-"))
        if not normalized_locale or not is_valid_locale(normalized_locale):
            errors.append((index + 1, tg_id_raw, locale_raw, "locale must be in format xx or xx-YY"))
            continue
        accepted.append((int(tg_id_raw), normalized_locale))
    return accepted, errors


def validate_telegram_token(bot_username: str, token: str) -> Dict:
    token = token.strip()
    if not TOKEN_RE.match(token):
        return {"ok": False, "reason": "invalid_format"}
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    except requests.RequestException:
        return {"ok": False, "reason": "invalid_token"}
    if resp.status_code != 200:
        return {"ok": False, "reason": "invalid_token"}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "reason": "invalid_token"}
    if not data.get("ok"):
        return {"ok": False, "reason": "invalid_token"}
    result = data.get("result", {})
    real_username = _normalize_username(result.get("username", ""))
    if not real_username:
        return {"ok": False, "reason": "invalid_token"}
    entered_username = _normalize_username(bot_username)
    if real_username != entered_username:
        return {
            "ok": False,
            "reason": "username_mismatch",
            "actual_username": real_username,
            "bot_id": result.get("id"),
        }
    return {
        "ok": True,
        "username": real_username,
        "id": result.get("id"),
        "name": result.get("first_name") or result.get("name") or "",
    }


class BotValidationRequest(BaseModel):
    username: str
    token: str


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


def _pricing_locales(locale_counts: List[Tuple[str, int]]) -> List[Dict[str, int]]:
    if not locale_counts:
        return []
    primary = [{"locale": locale, "total": total} for locale, total in locale_counts if total >= 1000]
    if primary:
        return primary
    top_locale, top_total = max(locale_counts, key=lambda row: row[1])
    return [{"locale": top_locale, "total": top_total}]


def build_wizard_context(
    db: Session,
    bot: Bot | None,
    step: int,
    request: Request,
    owner: BotOwner | None = None,
    errors: Dict | None = None,
    test_results: List[Dict] | None = None,
    test_summary: Dict | None = None,
) -> Dict:
    verification = None
    pricing_rows = []
    locale_summary = []
    other_locales = None
    locale_counts = []
    pricing_locales = []
    locale_stats = []
    if bot:
        verification = db.query(BotVerification).filter_by(bot_id=bot.id).first()
        pricing_rows = db.query(BotPricing).filter_by(bot_id=bot.id).all()
        locale_summary = db.execute(
            text(
                """
            SELECT locale,
                   COUNT(*) as total,
                   SUM(CASE WHEN verification_status = 'OK' THEN 1 ELSE 0 END) as ok,
                   SUM(CASE WHEN verification_status = 'NOT_STARTED' THEN 1 ELSE 0 END) as not_started,
                   SUM(CASE WHEN verification_status = 'BLOCKED' THEN 1 ELSE 0 END) as blocked
            FROM audience
            WHERE bot_id = :bot_id
            GROUP BY locale
                """
            ),
            {"bot_id": bot.id},
        ).fetchall()
        locale_summary, other_locales = compute_locale_summary(locale_summary)
        locale_counts = db.execute(
            text(
                """
            SELECT locale, COUNT(*) as total
            FROM audience
            WHERE bot_id = :bot_id
            GROUP BY locale
            ORDER BY total DESC
                """
            ),
            {"bot_id": bot.id},
        ).fetchall()
        pricing_locales = _pricing_locales(locale_counts)
        locale_stats = db.execute(
            text(
                """
            SELECT locale,
                   COUNT(*) as total,
                   SUM(CASE WHEN verification_status = 'OK' THEN 1 ELSE 0 END) as verified,
                   MAX(last_verified_at) as last_verified_at
            FROM audience
            WHERE bot_id = :bot_id
            GROUP BY locale
                """
            ),
            {"bot_id": bot.id},
        ).fetchall()
        locale_stats = sorted(
            locale_stats,
            key=lambda row: (0 if row[2] else 1, -(row[1] or 0)),
        )
    return {
        "request": request,
        "owner": owner,
        "bot": bot,
        "verification": verification,
        "locale_summary": locale_summary,
        "other_locales": other_locales,
        "locale_counts": locale_counts,
        "locale_stats": locale_stats,
        "pricing_rows": pricing_rows,
        "pricing_locales": pricing_locales,
        "step": step,
        "errors": errors,
        "test_results": test_results or [],
        "test_summary": test_summary or {},
        "can_finish": bool(
            bot
            and request.session.get("last_test_success_bot_id") == bot.id
        ),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/bot-owner/bots")
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
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Google did not return an email address")
    if not gmail_only(email):
        raise HTTPException(status_code=403, detail="Only Gmail accounts are allowed")
    get_owner(db, email)
    request.session["user"] = email
    return RedirectResponse("/bot-owner")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.post("/api/bots/validate-token")
async def validate_bot_token(request: Request, payload: BotValidationRequest):
    require_login(request)
    username = payload.username.strip()
    token = payload.token.strip()
    if not validate_bot_username(username):
        return {"ok": False, "reason": "invalid_username"}
    data = validate_telegram_token(username, token)
    if not data.get("ok"):
        return data
    return {
        "ok": True,
        "bot_username": f"@{data['username']}",
        "bot_id": data["id"],
        "bot_name": data["name"],
    }


@app.get("/bot-owner", response_class=HTMLResponse)
async def bot_owner_portal_redirect(request: Request):
    require_login(request)
    return RedirectResponse("/bot-owner/bots")


@app.get("/bot-owner/bots", response_class=HTMLResponse)
async def bot_owner_bots(request: Request, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    if not owner:
        owner = get_owner(db, email)
    bots = (
        db.query(Bot)
        .filter(Bot.owner_id == owner.id, Bot.deleted_at.is_(None))
        .order_by(Bot.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "bots_list.html",
        {
            "request": request,
            "bots": bots,
        },
    )


@app.get("/bot-owner/bots/new", response_class=HTMLResponse)
async def bot_owner_new(request: Request, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    if not owner:
        owner = get_owner(db, email)
    context = build_wizard_context(db, None, 1, request, owner=owner)
    return templates.TemplateResponse("bot_wizard.html", context)


@app.get("/bot-owner/bots/{bot_id}", response_class=HTMLResponse)
async def bot_owner_wizard(request: Request, bot_id: int, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    if not owner:
        owner = get_owner(db, email)
    bot = (
        db.query(Bot)
        .filter(Bot.id == bot_id, Bot.owner_id == owner.id, Bot.deleted_at.is_(None))
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    step = int(request.query_params.get("step", 1))
    if bot.token_needs_update and step > 1:
        return RedirectResponse(
            f"/bot-owner/bots/{bot.id}?step=1&token_update_required=1",
            status_code=303,
        )
    context = build_wizard_context(db, bot, step, request, owner=owner)
    return templates.TemplateResponse("bot_wizard.html", context)


@app.get("/bot-owner/bots/list", response_class=HTMLResponse)
async def bot_owner_bots_alias(request: Request, db: Session = Depends(get_db)):
    return await bot_owner_bots(request, db)


@app.post("/bot-owner/bots")
async def create_bot(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    token: str = Form(...),
    max_pushes_per_user_per_day: int = Form(1),
    token_validated: str = Form("false"),
):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    errors = {}
    if not validate_bot_username(username):
        errors["username"] = "Bot username must look like @mybot and end with 'bot'."
    if errors:
        context = build_wizard_context(db, None, 1, request, owner=owner, errors=errors)
        return templates.TemplateResponse("bot_wizard.html", context, status_code=400)
    normalized_username = f"@{_normalize_username(username)}"
    validation = validate_telegram_token(username, token)
    if token_validated.lower() != "true":
        errors["token"] = "Please validate the token before saving."
    elif not validation.get("ok"):
        if validation.get("reason") == "username_mismatch":
            actual = validation.get("actual_username", "unknown")
            errors["token"] = f"Token belongs to @{actual}, not {normalized_username}"
        else:
            errors["token"] = "Invalid Telegram token"
    if errors:
        context = build_wizard_context(db, None, 1, request, owner=owner, errors=errors)
        return templates.TemplateResponse("bot_wizard.html", context, status_code=400)
    try:
        encrypted_token = encrypt_token(token)
    except RuntimeError:
        raise HTTPException(status_code=500, detail="Encryption configuration error")
    try:
        status, bot = apply_bot_save(
            db,
            owner,
            normalized_username,
            encrypted_token,
            max_pushes_per_user_per_day,
        )
    except IntegrityError:
        db.rollback()
        status, bot = apply_bot_save(
            db,
            owner,
            normalized_username,
            encrypted_token,
            max_pushes_per_user_per_day,
        )
    if status == "duplicate":
        errors["username"] = "This bot is already connected to your account."
        context = build_wizard_context(db, None, 1, request, owner=owner, errors=errors)
        return templates.TemplateResponse("bot_wizard.html", context, status_code=409)
    return RedirectResponse(f"/bot-owner/bots/{bot.id}?step=1&saved=1", status_code=303)


@app.post("/bot-owner/bots/{bot_id}/token")
async def update_bot_token(
    request: Request,
    bot_id: int,
    db: Session = Depends(get_db),
    token: str = Form(...),
    token_validated: str = Form("false"),
):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    if not owner:
        owner = get_owner(db, email)
    bot = (
        db.query(Bot)
        .filter(Bot.id == bot_id, Bot.owner_id == owner.id, Bot.deleted_at.is_(None))
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    errors = {}
    validation = validate_telegram_token(bot.username, token)
    if token_validated.lower() != "true":
        errors["token"] = "Please validate the token before saving."
    elif not validation.get("ok"):
        if validation.get("reason") == "username_mismatch":
            actual = validation.get("actual_username", "unknown")
            errors["token"] = f"Token belongs to @{actual}, not {bot.username}"
        else:
            errors["token"] = "Invalid Telegram token"
    if errors:
        context = build_wizard_context(db, bot, 1, request, owner=owner, errors=errors)
        return templates.TemplateResponse("bot_wizard.html", context, status_code=400)
    try:
        encrypted_token = encrypt_token(token)
    except RuntimeError:
        raise HTTPException(status_code=500, detail="Encryption configuration error")
    bot.token_encrypted = encrypted_token
    bot.token_needs_update = False
    db.commit()
    return RedirectResponse(f"/bot-owner/bots/{bot.id}?step=1&token_updated=1", status_code=303)


@app.post("/bot-owner/bots/{bot_id}/upload")
async def upload_audience(
    request: Request,
    bot_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = (
        db.query(Bot)
        .filter(Bot.id == bot_id, Bot.owner_id == owner.id, Bot.deleted_at.is_(None))
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.token_needs_update:
        return RedirectResponse(
            f"/bot-owner/bots/{bot.id}?step=1&token_update_required=1",
            status_code=303,
        )

    content = await file.read()
    accepted_rows, errors = parse_audience_rows(content)
    accepted = 0
    total = len(accepted_rows) + len(errors)
    locale_counter = Counter()
    for tg_id, locale in accepted_rows:
        exists = db.query(Audience).filter_by(bot_id=bot_id, tg_id=tg_id).first()
        if exists:
            locale_counter[locale] += 1
            continue
        audience = Audience(bot_id=bot_id, tg_id=tg_id, locale=locale)
        db.add(audience)
        accepted += 1
        locale_counter[locale] += 1
    db.commit()

    total_users = db.query(Audience).filter_by(bot_id=bot_id).count()
    ru_users = (
        db.query(Audience)
        .filter(Audience.bot_id == bot_id, Audience.locale.like("ru%"))
        .count()
    )
    bot.audience_total = total_users
    bot.audience_ru = ru_users
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
                status=VerificationRunStatus.FAILED,
                total_users=total_users,
                eta_seconds=eta_seconds,
            )
            db.add(verification)
        else:
            verification.total_users = total_users
            verification.eta_seconds = eta_seconds
        db.commit()

    params = f"bot_id={bot_id}&uploaded=1"
    if error_report_id:
        params += f"&error_report_id={error_report_id}"
    params += f"&total={total}&accepted={accepted}&rejected={len(errors)}"
    return RedirectResponse(f"/bot-owner/bots/{bot_id}?step=2&{params}", status_code=303)


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
    if bot.token_needs_update:
        return {"status": "TOKEN_UPDATE_REQUIRED"}
    verification = db.query(BotVerification).filter_by(bot_id=bot_id).first()
    if not verification:
        return {"status": "NONE"}
    remaining = max(verification.total_users - verification.verified_users, 0)
    eta_seconds = int(remaining / 15)
    locale_stats = db.execute(
        text(
            """
        SELECT locale,
               COUNT(*) as total,
               SUM(CASE WHEN verification_status = 'OK' THEN 1 ELSE 0 END) as ok,
               SUM(CASE WHEN verification_status = 'BLOCKED' THEN 1 ELSE 0 END) as blocked,
               SUM(CASE WHEN verification_status = 'OTHER_ERROR' THEN 1 ELSE 0 END) as failed
        FROM audience
        WHERE bot_id = :bot_id
        GROUP BY locale
            """
        ),
        {"bot_id": bot_id},
    ).fetchall()
    return {
        "status": verification.status,
        "total": verification.total_users,
        "verified": verification.verified_users,
        "ok": verification.ok_count,
        "blocked": verification.blocked_count,
        "not_started": verification.not_started_count,
        "other_error": verification.other_error_count,
        "eta_seconds": eta_seconds,
        "locales": [
            {
                "locale": row[0],
                "total": row[1],
                "ok": row[2],
                "blocked": row[3],
                "failed": row[4],
            }
            for row in locale_stats
        ],
    }


@app.post("/bot-owner/bots/{bot_id}/verify/start")
async def start_verification_job(request: Request, bot_id: int, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = db.query(Bot).filter_by(id=bot_id, owner_id=owner.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.token_needs_update:
        return RedirectResponse(
            f"/bot-owner/bots/{bot.id}?step=1&token_update_required=1",
            status_code=303,
        )
    total_users = db.query(Audience).filter_by(bot_id=bot_id).count()
    eta_seconds = int(total_users / 15) if total_users else 0
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
    return RedirectResponse(f"/bot-owner/bots/{bot_id}?step=2&verification_started=1", status_code=303)


@app.post("/bot-owner/bots/{bot_id}/verify/locale")
async def start_verification_locale(
    request: Request,
    bot_id: int,
    locale: str = Form(...),
    db: Session = Depends(get_db),
):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = db.query(Bot).filter_by(id=bot_id, owner_id=owner.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.token_needs_update:
        return RedirectResponse(
            f"/bot-owner/bots/{bot.id}?step=1&token_update_required=1",
            status_code=303,
        )
    start_verification_for_locale.delay(bot_id, locale)
    return RedirectResponse(
        f"/bot-owner/bots/{bot_id}?step=2&verification_started=1",
        status_code=303,
    )


@app.post("/bot-owner/bots/{bot_id}/delete")
async def delete_bot(request: Request, bot_id: int, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = (
        db.query(Bot)
        .filter(Bot.id == bot_id, Bot.owner_id == owner.id, Bot.deleted_at.is_(None))
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    bot.deleted_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/bot-owner/bots", status_code=303)


@app.post("/bot-owner/bots/{bot_id}/pricing")
async def save_pricing(request: Request, bot_id: int, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = db.query(Bot).filter_by(id=bot_id, owner_id=owner.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.token_needs_update:
        return RedirectResponse(
            f"/bot-owner/bots/{bot.id}?step=1&token_update_required=1",
            status_code=303,
        )
    form = await request.form()
    max_pushes_raw = form.get("max_pushes_per_user_per_day")
    if not max_pushes_raw or int(max_pushes_raw) <= 0:
        raise HTTPException(status_code=400, detail="Max pushes per user must be at least 1.")
    locale_inputs = [key for key in form.keys() if key.startswith("locale_")]
    enabled_locales = 0
    for locale_key in locale_inputs:
        locale = locale_key.replace("locale_", "")
        cpm_raw = form.get(f"cpm_{locale}")
        enabled_locales += 1
        if not cpm_raw or int(cpm_raw) <= 0:
            raise HTTPException(status_code=400, detail="CPM must be positive")
        pricing = db.query(BotPricing).filter_by(bot_id=bot_id, locale=locale).first()
        if not pricing:
            pricing = BotPricing(bot_id=bot_id, locale=locale)
            db.add(pricing)
        pricing.is_for_sale = True
        pricing.cpm_cents = int(cpm_raw)
    if enabled_locales == 0:
        raise HTTPException(status_code=400, detail="At least one locale must be for sale")
    bot.max_pushes_per_user_per_day = int(max_pushes_raw)
    db.commit()
    return RedirectResponse(f"/bot-owner/bots/{bot_id}?step=3&pricing_saved=1", status_code=303)


@app.get("/bot-owner/bots/{bot_id}/test-push", response_class=HTMLResponse)
async def test_push_page(request: Request, bot_id: int, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    if not owner:
        owner = get_owner(db, email)
    bot = (
        db.query(Bot)
        .filter(Bot.id == bot_id, Bot.owner_id == owner.id, Bot.deleted_at.is_(None))
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    context = build_wizard_context(db, bot, 4, request, owner=owner)
    return templates.TemplateResponse("bot_wizard.html", context)


@app.post("/bot-owner/bots/{bot_id}/finish")
async def finish_bot_setup(request: Request, bot_id: int, db: Session = Depends(get_db)):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = db.query(Bot).filter_by(id=bot_id, owner_id=owner.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if request.session.get("last_test_success_bot_id") != bot.id:
        raise HTTPException(status_code=400, detail="Send at least one successful test push first.")
    return RedirectResponse("/bot-owner/bots", status_code=303)


@app.post("/bot-owner/bots/{bot_id}/test-push")
async def test_push(
    request: Request,
    bot_id: int,
    db: Session = Depends(get_db),
    tg_ids: str = Form(...),
    message: str = Form(...),
):
    email = require_login(request)
    owner = db.query(BotOwner).filter_by(email=email).first()
    bot = db.query(Bot).filter_by(id=bot_id, owner_id=owner.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if bot.token_needs_update:
        return RedirectResponse(
            f"/bot-owner/bots/{bot.id}?step=1&token_update_required=1",
            status_code=303,
        )
    if not allowed_html(message):
        raise HTTPException(status_code=400, detail="Message contains unsupported HTML tags")
    last_sent = request.session.get("last_test_push")
    now = time.time()
    if last_sent and now - last_sent < TEST_PUSH_RATE_LIMIT_SECONDS:
        raise HTTPException(status_code=429, detail="Please wait before sending another test")
    request.session["last_test_push"] = now
    try:
        token = decrypt_token(bot.token_encrypted)
    except RuntimeError:
        raise HTTPException(status_code=500, detail="Encryption configuration error")
    raw_ids = re.split(r"[,\n\r]+", tg_ids)
    parsed_ids = [item.strip() for item in raw_ids if item.strip()]
    results = []
    success_count = 0
    fail_count = 0
    for raw_id in parsed_ids:
        if not raw_id.isdigit():
            results.append({"tg_id": raw_id, "status": "invalid", "detail": "Invalid tg_id"})
            fail_count += 1
            continue
        tg_id_value = int(raw_id)
        audience = (
            db.query(Audience)
            .filter_by(bot_id=bot_id, tg_id=tg_id_value, verification_status=VerificationStatus.OK)
            .first()
        )
        if not audience:
            results.append({"tg_id": tg_id_value, "status": "not_verified", "detail": "User not verified"})
            fail_count += 1
            continue
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": tg_id_value, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            data = resp.json()
        except requests.RequestException:
            results.append({"tg_id": tg_id_value, "status": "failed", "detail": "Network error"})
            fail_count += 1
            continue
        if not data.get("ok"):
            description = data.get("description", "Send failed")
            if "unauthorized" in description.lower():
                bot.token_needs_update = True
                db.commit()
                results.append(
                    {
                        "tg_id": tg_id_value,
                        "status": "failed",
                        "detail": "Token expired. Please update it in Step 1.",
                    }
                )
                fail_count += 1
                continue
            results.append(
                {
                    "tg_id": tg_id_value,
                    "status": "failed",
                    "detail": description,
                }
            )
            fail_count += 1
            continue
        results.append({"tg_id": tg_id_value, "status": "ok", "detail": "Sent"})
        success_count += 1
    if success_count > 0:
        request.session["last_test_success_bot_id"] = bot.id
    summary = {"success": success_count, "failed": fail_count, "total": len(parsed_ids)}
    context = build_wizard_context(
        db,
        bot,
        4,
        request,
        owner=owner,
        test_results=results,
        test_summary=summary,
    )
    return templates.TemplateResponse("bot_wizard.html", context)
