# WC 2026 Prediction Notifier ⚽

A web app that sends you a **Telegram notification** at the end of every World Cup 2026 match, telling you whether your prediction was correct — and whether you nailed the exact score.

Built for group betting pools that use the [Excel Porra Mundial](https://matejero.es/excel-porra-mundial/) template.

---

## How it works

```
User uploads Excel file + Telegram chat ID
            │
            ▼
    ┌───────────────┐  every 60 s   ┌──────────────────────────┐
    │   FastAPI app │ ────────────► │  football-data.org API    │
    │   + SQLite    │               │  (match status + scores)  │
    └───────┬───────┘               └──────────────────────────┘
            │                              │
            │  status = FINISHED?          │ score available?
            │                              │
            │          NO score yet        ▼
            │◄──────────────────  ┌──────────────────────┐
            │                     │  ESPN scoreboard API  │
            │  score resolved      │  (fallback scores)   │
            │◄────────────────── └──────────────────────┘
            │
            ▼
    ┌───────────────┐
    │  Telegram Bot │ ──► "⚽ España 2–1 Francia  ✅ Correct!"
    └───────────────┘
```

Each user uploads their own Excel file and provides their personal Telegram chat ID. A single shared Telegram bot sends everyone their individual results.

**Why two APIs?** football-data.org (free tier) updates the match status to `FINISHED` quickly but can take 30–60 minutes to populate the actual score. To avoid that delay, whenever a match is marked finished but has no score yet, the app falls back to ESPN's scoreboard API — which typically has the final score within a minute of the final whistle. No ESPN account or API key is required.

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

#### HTTPS with Nginx (optional)

HTTPS is handled by an optional Nginx container using the `https` Docker Compose profile. You need your own certificate files (e.g. from Let's Encrypt or a CA).

1. Add the following to your `.env`:

   ```env
   SSL_CERT=/path/to/fullchain.pem
   SSL_KEY=/path/to/privkey.pem
   ```

2. Start with the `https` profile:

   ```bash
   docker compose --profile https up -d
   ```

   Nginx listens on ports `80` (redirects to HTTPS) and `443` (proxies to the app). To use non-standard ports, set `HTTP_PORT` and `HTTPS_PORT` in `.env`.

Without the `https` profile, only the app is started (plain HTTP on port `8000`, or `APP_PORT` if set).

---

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather | **required** |
| `FOOTBALL_DATA_API_KEY` | Key from football-data.org | **required** |
| `ADMIN_TELEGRAM_CHAT_ID` | Admin's Telegram chat ID (see below) | optional |
| `DATABASE_URL` | SQLite path | `sqlite:///./data/app.db` |
| `POLL_INTERVAL_SECONDS` | How often to check for results | `300` (5 min) |
| `DEBUG` | Enable FastAPI debug mode | `false` |
| `APP_PORT` | Host port the web container is exposed on | `8000` |
| `HTTP_PORT` | Nginx HTTP port (https profile only) | `80` |
| `HTTPS_PORT` | Nginx HTTPS port (https profile only) | `443` |
| `SSL_CERT` | Path to TLS certificate file (https profile only) | — |
| `SSL_KEY` | Path to TLS private key file (https profile only) | — |

### Admin chat ID

Set `ADMIN_TELEGRAM_CHAT_ID` to your personal Telegram chat ID to unlock two things:

- **New-user alerts** — you get a Telegram message whenever someone registers, including their name and chat ID.
- **`/users` command** — send `/users` to the bot to see the full list of registered users with their total points. Any other user who tries `/users` gets a permission-denied reply.

To find your chat ID, start the bot and send it `/chatid`.

### Scoring

Points are awarded per match and are configurable per tournament stage. The default scoring matches the standard Porra Mundial rules:

| Points for… | Default |
|-------------|---------|
| Correct sign (1X2) | 1 |
| Correct goal difference (sign must be correct) | 1 |
| Exact score (in addition to the above) | 2 |
| **Max per match** | **4** |

Points are **cumulative**: a correct sign earns 1 pt; same goal difference adds 1 more; exact score adds 2 more (total 4 pts).

Each tournament stage can have independent values via env vars:

```
POINTS_GROUP_SIGN / POINTS_GROUP_GOAL_DIFF / POINTS_GROUP_EXACT
POINTS_R32_SIGN   / POINTS_R32_GOAL_DIFF   / POINTS_R32_EXACT
POINTS_R16_SIGN   / POINTS_R16_GOAL_DIFF   / POINTS_R16_EXACT
POINTS_QF_SIGN    / POINTS_QF_GOAL_DIFF    / POINTS_QF_EXACT
POINTS_SF_SIGN    / POINTS_SF_GOAL_DIFF    / POINTS_SF_EXACT
POINTS_3RD_SIGN   / POINTS_3RD_GOAL_DIFF   / POINTS_3RD_EXACT
POINTS_FINAL_SIGN / POINTS_FINAL_GOAL_DIFF / POINTS_FINAL_EXACT
```

All default to `1 / 1 / 2`. Only set the ones that differ from your group's rules.

---

## Onboarding

### Admin (one-time setup)

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Deploy the app (Railway or Docker Compose) with `TELEGRAM_BOT_TOKEN` and `FOOTBALL_DATA_API_KEY` set.
3. Find your own chat ID: start the bot and send it `/chatid`. It replies with your numeric ID.
4. Set `ADMIN_TELEGRAM_CHAT_ID` to that ID and redeploy. You'll now receive new-user alerts and can run `/users`.
5. Share the app URL with your group.

### Users (share this with your group)

1. **Get your chat ID** — start the bot on Telegram and send it `/chatid`. It replies with your numeric ID.
2. **Open the app** — go to the deployed URL, fill in your name, your chat ID, and upload your Excel Porra Mundial `.xlsx` file.
3. **Done.** You'll receive a Telegram notification after each match finishes.

To update your predictions (e.g. after filling in knockout stage picks), just re-submit the form — it overwrites your previous entry.

### Bot commands

| Command | Who | Description |
|---------|-----|-------------|
| `/chatid` | anyone | Returns your Telegram chat ID |
| `/predictions` | registered users | Full predictions list ordered by kickoff time, with scores and points for finished matches |
| `/results` | registered users | Finished matches only, with prediction outcome and points earned |
| `/status` | registered users | Summary: correct/exact counts and total points accumulated |
| `/users` | admin only | All registered users with their total points |

---

## Notification examples

```
⚽ Match finished
España 2–1 Francia

Your prediction: España wins (2–1)
Exact score! 🎯  +4 pts  (total: 12 pts)
```

```
⚽ Match finished
Brasil 1–2 Argentina

Your prediction: Brasil wins (1–1)
Wrong prediction ❌  +0 pts  (total: 8 pts)
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
