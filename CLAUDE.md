# CLAUDE.md — Project Guide

## After every code change

1. **Rebuild and restart Docker**: run `docker compose up --build -d` from the project root.
2. **Keep docs in sync**: if the change affects bot commands, environment variables, scoring logic, admin setup, user onboarding, API endpoints, or project structure — update the relevant sections of `README.md` and this file. Only update what changed; don't rewrite accurate sections.

---

## Project overview

**WC 2026 Prediction Notifier** — a FastAPI app that sends personalised Telegram notifications after each World Cup 2026 match, comparing the result against a user's predictions from an Excel file ([Excel Porra Mundial](https://matejero.es/excel-porra-mundial/) template).

Deployed on this machine via Docker Compose. Database is SQLite, persisted in `data/app.db`.

---

## Architecture

```
app/
├── main.py          # FastAPI app, lifespan (DB init, scheduler, bot polling)
├── config.py        # Pydantic Settings (env vars, scoring config)
├── models.py        # SQLAlchemy models: User, NotifiedMatch
├── database.py      # SQLAlchemy engine / SessionLocal
├── scheduler.py     # APScheduler: polls football-data.org every POLL_INTERVAL_SECONDS
├── notifier.py      # Core match-checking + Telegram send logic
├── telegram_bot.py  # Long-polling bot: handles all /commands
├── excel_parser.py  # Reads predictions from .xlsx (WORLDCUP + Home sheets)
├── football_api.py  # football-data.org API client
├── espn_api.py      # ESPN scoreboard fallback (no key needed)
├── team_names.py    # Spanish → English team name mapping
└── templates/       # Jinja2 HTML (registration UI)
```

**Flow**: scheduler polls football-data.org → match FINISHED but no score → ESPN fallback → score resolved → compare against user predictions → send Telegram notification per user. Users who didn't predict a match also get a "no prediction, +0 pts" notification, but only for matches finished within `NO_PREDICTION_MAX_AGE_HOURS` (avoids back-filling the whole tournament).

---

## Key models

**`User`** — `id`, `name`, `telegram_chat_id` (unique), `predictions` (JSON), `utc_offset_hours`, `created_at`, `updated_at`

**`NotifiedMatch`** — one row per (user, match). Stores result (`home_score`, `away_score`, `duration`, `winner`, `stage`), user's prediction (`prediction`, `predicted_home_goals`, `predicted_away_goals`), outcome (`correct` 0/1/2, `points`), and `notified_at`. Unique on `(telegram_chat_id, api_match_id)`.

`correct`: `0` = wrong, `1` = correct sign/result, `2` = exact score.

---

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather | **required** |
| `FOOTBALL_DATA_API_KEY` | Key from football-data.org | **required** |
| `ADMIN_TELEGRAM_CHAT_ID` | Admin's Telegram chat ID | optional |
| `DATABASE_URL` | SQLite path | `sqlite:///./data/app.db` |
| `POLL_INTERVAL_SECONDS` | Match-check frequency | `300` |
| `SCORE_RECHECK_DELAY_SECONDS` | Seconds after notification to re-check score for corrections | `600` |
| `NO_PREDICTION_MAX_AGE_HOURS` | Notify users about finished matches they didn't predict (0 pts) only if finished within this many hours; `0` disables no-prediction notifications | `48` |
| `DEBUG` | FastAPI debug mode | `false` |
| `APP_PORT` | Host port for web container | `8000` |
| `HTTP_PORT` | Nginx HTTP port (https profile) | `80` |
| `HTTPS_PORT` | Nginx HTTPS port (https profile) | `443` |
| `SSL_CERT` / `SSL_KEY` | TLS cert/key paths (https profile) | — |

Scoring env vars follow the pattern `POINTS_{STAGE}_{TYPE}` where stage is `GROUP/R32/R16/QF/SF/3RD/FINAL` and type is `SIGN/GOAL_DIFF/EXACT`. All default to `1/1/2`.

Advancement bonuses: `POINTS_{R32/R16/QF/SF/3RD}_ADVANCEMENT` (default `1` each), `POINTS_SF_LOSER_ADVANCEMENT` (default `1`, SF loser reaching the user's predicted 3rd-place match), `POINTS_FINAL_ADVANCEMENT` (default `5`, champion), `POINTS_FINAL_RUNNER_UP` (default `3`).

`POINTS_GROUP_RANK_POSITION` — points per correctly predicted group finishing position (1st/2nd/3rd/4th). Default `1`.

---

## Bot commands

| Command | Who | Description |
|---------|-----|-------------|
| `/chatid` | anyone | Returns the user's Telegram chat ID |
| `/rank` | registered | Group leaderboard ranked by total points |
| `/predictions` | registered | Upcoming predictions with kickoff times (🔮 pick / ⚪ no pick) |
| `/results` | registered | Past matches with outcome and points (🎯/✅/❌; 🎫 advancement-only for predicted pairings that never took place) |
| `/last` | registered | Last finished match with everyone's picks |
| `/next` | registered | Next unfinished match with everyone's current pick |
| `/status` | registered | Points summary (✅ ⚽ 🎯 ❌ 🎫 counts) |
| `/users` | admin only | All registered users with total points |
| `/delete <chat_id>` | admin only | Remove a user and all their match records |
| `/broadcast <text>` | admin only | Send a custom message to all registered users (admin included) |
| `/recheck [dry-run]` | admin only | Re-fetch match scores and recalculate R32 advancement bonuses; send corrections to affected users. `dry-run` previews changes without modifying anything |

---

## Scoring logic

Per match, points are the sum of two **independent** components (`points_breakdown` in `notifier.py`):

**Score prediction** (all stages) — cumulative:
- Correct sign (1X2) → `SIGN` points
- Correct goal difference (sign must be correct) → `+GOAL_DIFF` points
- Exact score → `+EXACT` points (in addition to the above)

Default: 1 / 1 / 2 → max 4 pts. Each tournament stage can have independent values.

The score-prediction sign/diff/exact is judged on the result **before penalties**. For knockout
predictions (`prediction` is a team name) the sign is derived from the predicted scoreline, not
from the advancing team — so a correct scoreline scores even if the advancing-team pick is wrong.

**Advancing team** (knockout only) — **matchup-independent**, like the Excel's "Equipo
clasificado" rules: the bonus is earned when the advancing team was predicted to reach the next
round anywhere in the user's bracket (round-label lookup over `user_predictions`), even if the
predicted pairing for the fixture was different or missing. Per stage:
- R32/R16/QF: `+ADVANCEMENT` if the winner is one of the user's advance picks for that round.
- SF: `+ADVANCEMENT` if the winner is in the user's predicted Final;
  `+SF_LOSER_ADVANCEMENT` (`POINTS_SF_LOSER_ADVANCEMENT`, default 1) if the loser is in the
  user's predicted 3rd-place match.
- 3rd place: `+ADVANCEMENT` if the winner is the user's predicted 3rd-place-match winner.
- Final: `+ADVANCEMENT` (champion, 5) if the winner is the user's predicted champion, and
  independently `+RUNNER_UP` (3) if the loser is the user's predicted losing finalist.

Because advancement is bracket-based, a user with no pairing prediction for a fixture can still
earn points — such rows are stored with `prediction = NULL` but non-zero `points`.

**Group rankings** are awarded only when all 6 of a group's matches are FINISHED — the standings
API counts in-play games as played, so its live table is never trusted mid-match. Predicted
standings are simulated from the user's group predictions; perfect ties (same pts/gd/gf) fall
back to the actual standings order, matching how the Excel resolves them.

**R32 qualification bonus** (per group in `GroupRankingAward.advancement_points`): 1 pt per actual
R32 qualifier that appears anywhere in the user's predicted R32 bracket (the 32 teams of their
`1/32` fixtures) — not the predicted top-3 of each group.

**Delivery vs. persistence**: match records, awards, and corrections are persisted even when
Telegram permanently rejects the message (4xx, e.g. "chat not found" for a user who never started
the bot), so leaderboards stay correct; transient failures (network, 5xx) retry next cycle.

Penalty-shootout handling lives in `football_api.get_matches`: `home_score`/`away_score` come from
`regularTime + extraTime` (the pre-penalty score), and `winner` is derived from the combined
`fullTime` when the API leaves it null. The stored `correct`/cv reflects scoreline accuracy only
(0/1/2, plus 3 for the Final runner-up); the advancing-team point lives in `points`. The recheck
paths (`_correct_match_scores`) refresh `duration`/`winner` and no-prediction rows too, so stored
results can't go permanently stale.

---

## Team name mapping

`app/team_names.py` maps Spanish names (from the Excel) to English names (football-data.org API). If a team is missing, the app falls back to kickoff-time matching. Add missing teams there.

---

## Deployment (this machine)

```bash
docker compose up --build -d   # rebuild and restart
docker compose logs -f web     # follow logs
docker compose ps              # check status
```

Database survives restarts via the `data/` volume mount. Migrations run automatically on startup (`_migrate_db` in `main.py` adds new columns without dropping data).
