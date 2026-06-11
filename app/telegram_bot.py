import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import NotifiedMatch, User
from .notifier import send_message
from .team_names import names_match, normalize, spanish_to_english

logger = logging.getLogger(__name__)

_MAX_MSG = 4000  # Telegram's 4096-char limit with some headroom


async def _get_updates(bot_token: str, offset: int) -> list[dict]:
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    async with httpx.AsyncClient(timeout=35.0) as client:
        try:
            r = await client.get(url, params={
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["message"],
            })
            return r.json().get("result", [])
        except httpx.RequestError as e:
            logger.error(f"getUpdates error: {e}")
            return []


def _build_status(user: User, notified: list[NotifiedMatch]) -> str:
    total_preds = len(user.predictions or {})
    finished = [n for n in notified if n.home_team and n.away_team]

    lines = [
        f"📊 <b>WC 2026 — {user.name}</b>",
        f"📋 Predictions loaded: <b>{total_preds} matches</b>",
    ]

    if not finished:
        lines.append("\nNo matches finished yet — stay tuned! ⏳")
        return "\n".join(lines)

    total_f = len(finished)
    correct_count = sum(1 for n in finished if (n.correct or 0) >= 1)
    exact_count = sum(1 for n in finished if (n.correct or 0) == 2)

    lines.append(
        f"🏆 Results: <b>{correct_count}/{total_f}</b> correct"
        + (f"  ({exact_count} exact 🎯)" if exact_count else "")
    )
    lines.append("")

    for n in sorted(finished, key=lambda x: x.notified_at or datetime.min.replace(tzinfo=timezone.utc)):
        score = f"{n.home_score}–{n.away_score}" if n.home_score is not None else "?–?"
        suffix = {"EXTRA_TIME": " aet", "PENALTY_SHOOTOUT": " pens"}.get(n.duration or "", "")
        result = f"{n.home_team} {score}{suffix} {n.away_team}"

        mark = {2: "🎯", 1: "✅", 0: "❌"}.get(n.correct, "⚪")

        if n.prediction == "home":
            pred_label = n.home_team
        elif n.prediction == "away":
            pred_label = n.away_team
        elif n.prediction == "draw":
            pred_label = "draw"
        elif n.prediction:
            pred_label = n.prediction  # knockout team name (Spanish)
        else:
            pred_label = "—"

        if n.predicted_home_goals is not None and n.predicted_away_goals is not None:
            pred_label += f" ({n.predicted_home_goals}–{n.predicted_away_goals})"

        lines.append(f"{mark} {result}  › {pred_label}")

    return "\n".join(lines)


async def _handle_status(chat_id: str, bot_token: str):
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_chat_id=chat_id).first()
        if not user:
            await send_message(
                bot_token, chat_id,
                "⚠️ You're not registered yet.\nVisit the app to upload your Excel predictions."
            )
            return

        notified = db.query(NotifiedMatch).filter_by(telegram_chat_id=chat_id).all()
        text = _build_status(user, notified)
    finally:
        db.close()

    # Send in chunks if the message is long (many finished matches)
    while text:
        chunk, text = text[:_MAX_MSG], text[_MAX_MSG:]
        await send_message(bot_token, chat_id, chunk)


def _prediction_label(prediction: str, home_es: str, away_es: str,
                       pred_hg: int | None, pred_ag: int | None) -> str:
    if prediction == "home":
        label = home_es
    elif prediction == "away":
        label = away_es
    elif prediction == "draw":
        label = "draw"
    else:
        label = prediction  # knockout team name (Spanish)
    if pred_hg is not None and pred_ag is not None:
        label += f" ({pred_hg}–{pred_ag})"
    return label


def _build_nm_lookup(notified: list[NotifiedMatch]) -> dict:
    """Map (norm_home_en, norm_away_en) → NotifiedMatch for fast lookup."""
    lookup = {}
    for nm in notified:
        if nm.home_team and nm.away_team:
            key = (normalize(nm.home_team), normalize(nm.away_team))
            lookup[key] = nm
    return lookup


def _prediction_line(match_num: int, pred: dict, nm_lookup: dict) -> str:
    home_es = pred.get("home_team", "?")
    away_es = pred.get("away_team", "?")
    prediction = pred.get("prediction") or "—"
    pred_hg = pred.get("predicted_home_goals")
    pred_ag = pred.get("predicted_away_goals")

    pred_label = _prediction_label(prediction, home_es, away_es, pred_hg, pred_ag)

    # Look up finished match by mapped English team names
    home_en = normalize(spanish_to_english(home_es))
    away_en = normalize(spanish_to_english(away_es))
    nm = nm_lookup.get((home_en, away_en))

    if nm and nm.home_score is not None:
        score = f"{nm.home_score}–{nm.away_score}"
        suffix = {"EXTRA_TIME": " aet", "PENALTY_SHOOTOUT": " pens"}.get(nm.duration or "", "")
        mark = {2: "🎯", 1: "✅", 0: "❌"}.get(nm.correct, "⚪")
        return f"{mark} {match_num:>3}. {nm.home_team} {score}{suffix} {nm.away_team}  › {pred_label}"
    else:
        return f"⚪ {match_num:>3}. {home_es} vs {away_es}  › {pred_label}"


def _chunk_messages(lines: list[str], max_len: int = _MAX_MSG) -> list[str]:
    """Pack lines into messages that each fit within max_len."""
    messages, current, current_len = [], [], 0
    for line in lines:
        needed = len(line) + (1 if current else 0)
        if current and current_len + needed > max_len:
            messages.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += needed
    if current:
        messages.append("\n".join(current))
    return messages


_KNOCKOUT_ROUNDS = [
    ("🔵 <b>Round of 32</b>",   range(73, 89)),
    ("🟡 <b>Round of 16</b>",   range(89, 97)),
    ("🟠 <b>Quarter-finals</b>", range(97, 101)),
    ("🔴 <b>Semi-finals</b>",   range(101, 103)),
    ("🥉 <b>Third place</b>",   range(103, 104)),
    ("🏆 <b>Final</b>",         range(104, 105)),
]


def _build_predictions(user: User, notified: list[NotifiedMatch]) -> list[str]:
    nm_lookup = _build_nm_lookup(notified)
    preds = user.predictions or {}
    sorted_preds = sorted((int(k), v) for k, v in preds.items())

    all_messages: list[str] = []

    # --- Group stage ---
    group_preds = [(n, p) for n, p in sorted_preds if n <= 72]
    lines = ["🏟 <b>Group Stage</b>"]
    for match_num, pred in group_preds:
        lines.append(_prediction_line(match_num, pred, nm_lookup))
    all_messages.extend(_chunk_messages(lines))

    # --- Knockout rounds ---
    for title, rng in _KNOCKOUT_ROUNDS:
        round_preds = [(n, p) for n, p in sorted_preds if n in rng]
        if not round_preds:
            continue
        lines = [title]
        for match_num, pred in round_preds:
            lines.append(_prediction_line(match_num, pred, nm_lookup))
        all_messages.extend(_chunk_messages(lines))

    return all_messages


async def _handle_predictions(chat_id: str, bot_token: str):
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_chat_id=chat_id).first()
        if not user:
            await send_message(
                bot_token, chat_id,
                "⚠️ You're not registered yet.\nVisit the app to upload your Excel predictions."
            )
            return
        notified = db.query(NotifiedMatch).filter_by(telegram_chat_id=chat_id).all()
        messages = _build_predictions(user, notified)
    finally:
        db.close()

    for msg in messages:
        await send_message(bot_token, chat_id, msg)


async def _handle_update(update: dict, bot_token: str):
    message = update.get("message", {})
    text = (message.get("text") or "").strip()
    chat_id = str(message.get("chat", {}).get("id", ""))

    if not text.startswith("/") or not chat_id:
        return

    cmd = text.split("@")[0].split()[0]  # "/status@BotName arg" → "/status"

    if cmd == "/status":
        await _handle_status(chat_id, bot_token)
    elif cmd == "/predictions":
        await _handle_predictions(chat_id, bot_token)


async def send_predictions_to_user(chat_id: str, bot_token: str):
    """Send the full predictions list to a user (used after registration/update)."""
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_chat_id=chat_id).first()
        if not user:
            return
        notified = db.query(NotifiedMatch).filter_by(telegram_chat_id=chat_id).all()
        messages = _build_predictions(user, notified)
    finally:
        db.close()

    total = len(user.predictions or {})
    await send_message(bot_token, chat_id,
                       f"📥 <b>Predictions loaded!</b> ({total} matches)\nHere's your full list:")
    for msg in messages:
        await send_message(bot_token, chat_id, msg)


async def polling_loop(bot_token: str):
    """Long-poll Telegram for bot commands. Runs as a background asyncio task."""
    logger.info("Telegram bot polling started")
    offset = 0
    while True:
        try:
            updates = await _get_updates(bot_token, offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                await _handle_update(upd, bot_token)
        except asyncio.CancelledError:
            logger.info("Telegram bot polling stopped")
            break
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)
