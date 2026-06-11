# WC 2026 Prediction Notifier ⚽

A web app that sends you a **Telegram notification** at the end of every World Cup 2026 match, telling you whether your prediction was correct — and whether you nailed the exact score.

Built for group betting pools that use the [Excel Porra Mundial](https://matejero.es/excel-porra-mundial/) template.

---

## How it works

```
User uploads Excel file + Telegram chat ID
            │
            ▼
    ┌───────────────┐     every 5 min     ┌───────────────────────┐
    │   FastAPI app │ ──────────────────► │  football-data.org API │
    │   + SQLite    │                     │  (match results)       │
    └───────┬───────┘                     └───────────────────────┘
            │  match finished?
            ▼
    ┌───────────────┐
    │  Telegram Bot │ ──► "⚽ España 2–1 Francia  ✅ Correct!"
    └───────────────┘
```

Each user uploads their own Excel file and provides their personal Telegram chat ID. A single shared Telegram bot sends everyone their individual results.

---

## Prerequisites

| What | Where |
|------|-------|
| Python 3.12+ | — |
| Telegram bot token | Create one via [@BotFather](https://t.me/BotFather) — one token shared by the whole group |
| football-data.org API key | Free tier at [football-data.org/client/register](https://www.football-data.org/client/register) |

---

## Quickstart (local)

```bash
git clone https://github.com/emanuele-dedonatis/excel-porra-munidal-2026-notifier
cd excel-porra-munidal-2026-notifier

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # fill in TELEGRAM_BOT_TOKEN and FOOTBALL_DATA_API_KEY
mkdir -p data

uvicorn app.main:app --reload
# → open http://localhost:8000
```

---

## Deploy

### Railway (recommended — free tier)

1. Fork this repo
2. Create a new project at [railway.app](https://railway.app) → connect your fork
3. Add environment variables (see table below)
4. Deploy — Railway auto-detects the `Dockerfile`

Add a **Volume** mount at `/app/data` to persist the SQLite database across deploys.

### Docker Compose

```bash
cp .env.example .env   # fill in credentials
docker compose up -d
```

The `data/` directory is mounted as a volume so the database survives container restarts.

---

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather | **required** |
| `FOOTBALL_DATA_API_KEY` | Key from football-data.org | **required** |
| `DATABASE_URL` | SQLite path | `sqlite:///./data/app.db` |
| `POLL_INTERVAL_SECONDS` | How often to check for results | `300` (5 min) |
| `DEBUG` | Enable FastAPI debug mode | `false` |

---

## User onboarding (share this with your group)

1. **Get your chat ID** — send any message to [@userinfobot](https://t.me/userinfobot) on Telegram. It replies with your numeric ID instantly.
2. **Start the bot** — send a message to the group's Telegram bot (e.g. `/start`) so it can reach you.
3. **Open the app** — go to the deployed URL, fill in your name, chat ID, and upload your Excel Porra Mundial `.xlsx` file.
4. **Done.** You'll receive a Telegram notification after each match finishes.

To update your predictions (e.g. after filling in more knockout stage picks), just re-submit the form — it overwrites your previous entry.

---

## Notification examples

```
⚽ Match finished
España 2–1 Francia

Your prediction: España wins (2–1)
Exact score! 🎯
```

```
⚽ Match finished
Brasil 1–2 Argentina

Your prediction: Brasil wins
Wrong prediction ❌
```

---

## Excel format

The app reads predictions from the **WORLDCUP** sheet of the [Excel Porra Mundial 2026](https://matejero.es/excel-porra-mundial/) template:

| Column | Content |
|--------|---------|
| D (col 4) | Predicted outcome: `Local` / `Visitante` / `Empate` for group stage; winning team name for knockouts |
| AA (col 27) | Home team name (Spanish) |
| AF (col 32) | Away team name (Spanish) |
| AC (col 29) | Predicted home goals |
| AD (col 30) | Predicted away goals |
| AH (col 34) | Match number (1–104) |
| X (col 24) | Kickoff datetime (Spain UTC+2 timezone) |

Kickoff times are assumed to be in **UTC+2** (Spain summer time). If you use a different timezone in the Excel's Home sheet, set `utc_offset_hours` accordingly in `excel_parser.py`.

---

## Adding missing team names

The `app/team_names.py` file maps Spanish team names (Excel) to English names (football-data.org API). If a team name isn't in the mapping, the app falls back to kickoff time matching. To add a team:

```python
# app/team_names.py
SPANISH_TO_ENGLISH = {
    ...
    "Nombre en español": "English Name",
}
```

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Registration UI |
| `POST` | `/register` | Register or update a user |
| `GET` | `/health` | Health check |
