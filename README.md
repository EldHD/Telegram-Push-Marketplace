# Telegram Push Marketplace — Bot Owner Portal (Phase 1)

This repository contains the Phase 1 Bot Owner Portal for the Telegram Push Marketplace. Bot owners can connect their bots, upload audience CSVs, verify reachability via silent pings, set CPM pricing by locale, and send test pushes.

## Features

- Google OAuth login (Gmail-only)
- Bot onboarding with token validation via Telegram `getMe`
- Encrypted token storage (Fernet)
- Audience CSV upload with error report export
- Background verification with 15 req/s pacing and resume support
- Verification summaries and per-locale breakdowns
- CPM pricing per locale (enabled after verification)
- Test push with HTML validation and rate limiting

## Setup

### Requirements

- Docker + Docker Compose
- Google OAuth credentials
- A Fernet key

### Environment Variables

Set these variables before running:

```
export GOOGLE_CLIENT_ID=your_google_client_id
export GOOGLE_CLIENT_SECRET=your_google_client_secret
export FERNET_KEY=$(python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
)
```

### Run

```
docker compose up --build
```

Then open: http://localhost:8000/bot-owner

## Verification Logic

- When a CSV is uploaded, valid rows are inserted into the `audience` table and a background verification task starts.
- Verification uses Telegram `sendChatAction(chat_id, action="typing")` as a silent ping.
- Requests are paced at exactly **15 per second** in the worker.
- Status classification:
  - `OK` — reachable
  - `BLOCKED` — bot blocked by user
  - `NOT_STARTED` — chat not found / bot never started
  - `OTHER_ERROR` — any other error
- Progress is persisted after each user, and the worker resumes from the last processed `tg_id` after crashes or restarts.
- ETA is calculated as `remaining_users / 15` and refreshed on the UI.

## Architecture

- FastAPI + Jinja UI
- PostgreSQL data model
- Celery + Redis background worker
- Designed to add future Advertiser and Moderator portals without changing the core bot owner flow
