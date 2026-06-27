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
| `SCORE_RECHECK_DELAY_SECONDS` | Seconds after notification to re-check the score for corrections | `600` (10 min) |
| `DEBUG` | Enable FastAPI debug mode | `false` |
| `APP_PORT` | Host port the web container is exposed on | `8000` |
| `HTTP_PORT` | Nginx HTTP port (https profile only) | `80` |
| `HTTPS_PORT` | Nginx HTTPS port (https profile only) | `443` |
| `SSL_CERT` | Path to TLS certificate file (https profile only) | — |
| `SSL_KEY` | Path to TLS private key file (https profile only) | — |

### Admin chat ID

Set `ADMIN_TELEGRAM_CHAT_ID` to your personal Telegram chat ID to unlock:

- **New-user alerts** — you get a Telegram message whenever someone registers or updates their predictions, including their name and chat ID.
- **`/users` command** — full list of registered users with their total points.
- **`/delete <chat_id>` command** — remove a user and all their match history from the database (useful for re-registration or testing).

Any other user who tries these commands gets a permission-denied reply.

To find your chat ID, start the bot and send it `/chatid`.

### Scoring

#### Match results

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

#### Group stage rankings

Once all three matchdays of a group finish, the bot derives each user's **predicted group standings** by simulating the group table from their stored match predictions (predicted win/draw/loss → 3/1/0 pts, then sorted by goal difference and goals scored). It compares this with the actual API standings and awards 1 point per correctly predicted finishing position (1st / 2nd / 3rd / 4th).

Users receive one Telegram notification per group as it completes, and group ranking points are included in all totals shown by `/rank`, `/status`, and `/users`.

The per-position point value is configurable:

```
POINTS_GROUP_RANK_POSITION=1   # default: 1 pt per correct position
```

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
2. **Open the app** — go to the deployed URL, enter your chat ID, and upload your completed Excel Porra Mundial `.xlsx` file. Your name is read automatically from the file (Home sheet, cell C10).
3. **Done.** You'll receive a Telegram notification after each match finishes.

To update your predictions (e.g. after filling in knockout stage picks), just re-submit the form — it overwrites your previous entry.

### Registering commands with BotFather

To show the command list in Telegram, send `/setcommands` to [@BotFather](https://t.me/BotFather), select your bot, then paste:

```
chatid - Get your Telegram chat ID
rank - Leaderboard with points and prediction stats
predictions - Your full predictions list with kickoff times and results
results - Finished matches with your outcomes and points earned
status - Your points summary and prediction breakdown
last - Last match result with everyone's picks
next - Next match not finished yet with your current pick
```

### Bot commands

| Command | Who | Description |
|---------|-----|-------------|
| `/chatid` | anyone | Returns your Telegram chat ID |
| `/rank` | registered users | Group leaderboard ranked by total points, with ✅ ⚽ 🎯 ❌ breakdown per user |
| `/predictions` | registered users | Full predictions list ordered by kickoff time (in your local timezone), with scores and points for finished matches |
| `/results` | registered users | Finished matches only, with prediction outcome and points earned |
| `/last` | registered users | Last finished match result with every user's prediction and points |
| `/next` | registered users | Next match not finished yet with every user's current pick |
| `/status` | registered users | Points summary with ✅ sign / ⚽ goal diff / 🎯 exact / ❌ wrong counts |
| `/users` | admin only | All registered users with their total points |
| `/delete <chat_id>` | admin only | Remove a user and all their match records from the database |
| `/recheck` | admin only | Re-fetch all match scores from the API, send correction messages to affected users, and report a summary |

---

## Notifications

The bot sends the following automatic notifications. All are per-user and personalised.

### Match result

Sent after each match finishes and the score is confirmed.

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

Possible verdicts:

| Verdict | Condition |
|---------|-----------|
| Exact score! 🎯 | Predicted the exact scoreline |
| Correct result! ✅ | Predicted the right winner/draw |
| Runner-up bonus! 🥈 | Predicted the team that LOSES the Final |
| Wrong prediction ❌ | None of the above |

For knockout matches the notification also includes the advancement bonus (+1 to +5 pts depending on stage) stacked on top of the sign/diff/exact points when the prediction is correct.

### Score correction

Sent ~10 minutes after the match result if the score changes (e.g. a late API update). Shows the old score, new score, and revised points.

```
⚠️ Score correction
España 2–1 Francia (was 2–0)

Your prediction: España wins (2–1)
Exact score! 🎯  +4 pts  (pts: 2 → 4, total: 14 pts)
```

### Group final standings

Sent once per group, when all three matchdays of that group are complete. Shows the user's predicted order vs. the actual order, and the points earned for correctly placed teams.

```
🏆 Group A final standings

1st:  ✅  Germany  (you predicted: Germany)
2nd:  ✅  Scotland  (you predicted: Scotland)
3rd:  ❌  Switzerland  (you predicted: Hungary)
4th:  ❌  Hungary  (you predicted: Switzerland)

Group ranking: +2 pts  (total: 18 pts)
```

### R32 qualification bonuses

Sent once, after all 12 groups are complete and the Round of 32 fixtures are published. Shows for each group whether the user's predicted top-2 teams actually qualified.

```
🌍 R32 Qualification Bonuses

Group A:  ✅ Germany  ✅ Scotland  +2
Group B:  ✅ Spain  ❌ Croatia  +1
...
Group L:  ✅ France  ✅ Argentina  +2

+18 pts  (total: 56 pts)
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
| X (col 24) | Kickoff datetime in the user's local timezone |

The app also reads from the **Home** sheet:

| Cell | Content |
|------|---------|
| C8 | UTC offset in hours (e.g. `2` for UTC+2). Used to convert kickoff times to UTC and display them back in the user's local time. |
| C10 | Participant's name |

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
