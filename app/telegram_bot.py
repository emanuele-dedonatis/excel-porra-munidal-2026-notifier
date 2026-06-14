import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import func as sa_func
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


def _stats(matches: list[NotifiedMatch]) -> tuple[int, int, int, int, int]:
    """Return (total, sign_only, goal_diff, exact, total_pts) for a list of finished NotifiedMatches."""
    total = sign_only = goal_diff = exact = total_pts = 0
    for n in matches:
        total += 1
        total_pts += n.points or 0
        cv = n.correct or 0
        if cv == 0:
            continue
        is_exact = cv == 2
        is_diff = False
        if (n.predicted_home_goals is not None and n.predicted_away_goals is not None
                and n.home_score is not None and n.away_score is not None):
            is_diff = (n.predicted_home_goals - n.predicted_away_goals) == (n.home_score - n.away_score)
        if is_exact:
            exact += 1
        elif is_diff:
            goal_diff += 1
        else:
            sign_only += 1
    return total, sign_only, goal_diff, exact, total_pts


def _stats_line(total: int, sign_only: int, goal_diff: int, exact: int) -> str:
    wrong = total - sign_only - goal_diff - exact
    return f"✅ {sign_only + goal_diff + exact}  ⚽ {goal_diff + exact}  🎯 {exact}  ❌ {wrong}"


def _build_status(user: User, notified: list[NotifiedMatch]) -> str:
    finished = [n for n in notified if n.home_team and n.away_team]

    lines = [f"📊 <b>WC 2026 — {user.name}</b>"]

    if not finished:
        lines.append("\nNo matches finished yet — stay tuned! ⏳")
        return "\n".join(lines)

    total_f = len(finished)
    _, sign_only, goal_diff, exact, total_pts = _stats(finished)
    lines.append(_stats_line(total_f, sign_only, goal_diff, exact))
    lines.append(f"⭐ Total points: <b>{total_pts} pts</b>")
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


def _prediction_line(pred: dict, nm_lookup: dict, utc_offset: float = 0.0) -> str:
    home_es = pred.get("home_team", "?")
    away_es = pred.get("away_team", "?")
    prediction = pred.get("prediction") or "—"
    pred_hg = pred.get("predicted_home_goals")
    pred_ag = pred.get("predicted_away_goals")

    pred_label = _prediction_label(prediction, home_es, away_es, pred_hg, pred_ag)

    kickoff_str = "?"
    kickoff_iso = pred.get("kickoff_utc")
    if kickoff_iso:
        try:
            dt = datetime.fromisoformat(kickoff_iso) + timedelta(hours=utc_offset)
            kickoff_str = dt.strftime("%d %b %H:%M")
        except (ValueError, TypeError):
            pass

    # Look up finished match by mapped English team names
    home_en = normalize(spanish_to_english(home_es))
    away_en = normalize(spanish_to_english(away_es))
    nm = nm_lookup.get((home_en, away_en))

    if nm and nm.home_score is not None:
        score = f"{nm.home_score}–{nm.away_score}"
        suffix = {"EXTRA_TIME": " aet", "PENALTY_SHOOTOUT": " pens"}.get(nm.duration or "", "")
        mark = {2: "🎯", 1: "✅", 0: "❌"}.get(nm.correct, "⚪")
        pts_str = f"  +{nm.points} pts" if nm.points is not None else ""
        return f"{mark} {kickoff_str} {nm.home_team} {score}{suffix} {nm.away_team}  › {pred_label}{pts_str}"
    else:
        return f"⚪ {kickoff_str} {home_es} vs {away_es}  › {pred_label}"


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


def _kickoff_dt(pred: dict) -> datetime:
    iso = pred.get("kickoff_utc")
    if iso:
        try:
            return datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _find_prediction_by_match(user: User, home_en: str, away_en: str) -> dict | None:
    preds = user.predictions or {}
    for pred in preds.values():
        if (normalize(spanish_to_english(pred.get("home_team", ""))) == home_en
                and normalize(spanish_to_english(pred.get("away_team", ""))) == away_en):
            return pred
    return None


def _build_predictions(user: User, notified: list[NotifiedMatch]) -> list[str]:
    nm_lookup = _build_nm_lookup(notified)
    preds = user.predictions or {}
    sorted_preds = sorted(((int(k), v) for k, v in preds.items()), key=lambda x: _kickoff_dt(x[1]))
    utc_offset = user.utc_offset_hours or 0.0

    all_messages: list[str] = []

    # --- Group stage ---
    group_preds = [(n, p) for n, p in sorted_preds if n <= 72]
    lines = ["🏟 <b>Group Stage</b>"]
    for _match_num, pred in group_preds:
        lines.append(_prediction_line(pred, nm_lookup, utc_offset))
    all_messages.extend(_chunk_messages(lines))

    # --- Knockout rounds ---
    for title, rng in _KNOCKOUT_ROUNDS:
        round_preds = [(n, p) for n, p in sorted_preds if n in rng]
        if not round_preds:
            continue
        lines = [title]
        for _match_num, pred in round_preds:
            lines.append(_prediction_line(pred, nm_lookup, utc_offset))
        all_messages.extend(_chunk_messages(lines))

    return all_messages


def _build_results(user: User, notified: list[NotifiedMatch]) -> list[str]:
    nm_lookup = _build_nm_lookup(notified)
    preds = user.predictions or {}
    sorted_preds = sorted(((int(k), v) for k, v in preds.items()), key=lambda x: _kickoff_dt(x[1]))
    utc_offset = user.utc_offset_hours or 0.0

    def is_finished(pred: dict) -> bool:
        home_en = normalize(spanish_to_english(pred.get("home_team", "")))
        away_en = normalize(spanish_to_english(pred.get("away_team", "")))
        nm = nm_lookup.get((home_en, away_en))
        return nm is not None and nm.home_score is not None

    all_messages: list[str] = []

    group_preds = [(n, p) for n, p in sorted_preds if n <= 72 and is_finished(p)]
    if group_preds:
        lines = ["🏟 <b>Group Stage</b>"]
        for _match_num, pred in group_preds:
            lines.append(_prediction_line(pred, nm_lookup, utc_offset))
        all_messages.extend(_chunk_messages(lines))

    for title, rng in _KNOCKOUT_ROUNDS:
        round_preds = [(n, p) for n, p in sorted_preds if n in rng and is_finished(p)]
        if not round_preds:
            continue
        lines = [title]
        for _match_num, pred in round_preds:
            lines.append(_prediction_line(pred, nm_lookup, utc_offset))
        all_messages.extend(_chunk_messages(lines))

    if not all_messages:
        all_messages = ["No finished matches yet — stay tuned! ⏳"]

    return all_messages


async def _handle_results(chat_id: str, bot_token: str):
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
        messages = _build_results(user, notified)
    finally:
        db.close()

    for msg in messages:
        await send_message(bot_token, chat_id, msg)


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


async def notify_admin_new_user(admin_chat_id: str, bot_token: str, name: str, chat_id: str, is_new: bool = True):
    label = "New user registered" if is_new else "User updated predictions"
    await send_message(
        bot_token, admin_chat_id,
        f"👤 <b>{label}</b>\nName: {name}\nChat ID: <code>{chat_id}</code>"
    )


async def _handle_users(chat_id: str, bot_token: str, admin_chat_id: str):
    if chat_id != admin_chat_id:
        await send_message(bot_token, chat_id, "⛔ This command is restricted to the admin.")
        return

    db: Session = SessionLocal()
    try:
        users: list[User] = db.query(User).order_by(User.created_at).all()
        user_pts = {
            chat_id_: pts or 0
            for chat_id_, pts in db.query(
                NotifiedMatch.telegram_chat_id,
                sa_func.sum(NotifiedMatch.points),
            ).group_by(NotifiedMatch.telegram_chat_id).all()
        }
    finally:
        db.close()

    if not users:
        await send_message(bot_token, chat_id, "No users registered yet.")
        return

    lines = [f"👥 <b>Registered users ({len(users)})</b>"]
    for u in users:
        pts = user_pts.get(u.telegram_chat_id, 0)
        lines.append(f"• {u.name}  <code>{u.telegram_chat_id}</code>  ⭐ {pts} pts")

    await send_message(bot_token, chat_id, "\n".join(lines))


async def _handle_standings(chat_id: str, bot_token: str):
    db: Session = SessionLocal()
    try:
        users: list[User] = db.query(User).all()
        all_notified = db.query(NotifiedMatch).filter(NotifiedMatch.home_team.isnot(None)).all()
    finally:
        db.close()

    if not users:
        await send_message(bot_token, chat_id, "No users registered yet.")
        return

    by_user: dict[str, list[NotifiedMatch]] = {}
    for nm in all_notified:
        by_user.setdefault(nm.telegram_chat_id, []).append(nm)

    def sort_key(u: User):
        _, _, _, _, pts = _stats(by_user.get(u.telegram_chat_id, []))
        return pts

    ranked = sorted(users, key=sort_key, reverse=True)

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 <b>Standings</b>\n"]
    for i, u in enumerate(ranked, 1):
        total, sign_only, goal_diff, exact, pts = _stats(by_user.get(u.telegram_chat_id, []))
        prefix = medals.get(i, f"{i}.")
        lines.append(f"{prefix} <b>{u.name}</b>  ⭐ {pts} pts\n    {_stats_line(total, sign_only, goal_diff, exact)}")

    await send_message(bot_token, chat_id, "\n".join(lines))


async def _handle_last(chat_id: str, bot_token: str):
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_chat_id=chat_id).first()
        if not user:
            await send_message(bot_token, chat_id,
                               "⚠️ You're not registered yet.\nVisit the app to upload your Excel predictions.")
            return

        # Find the highest api_match_id (IDs are sequential, so highest = most recent match)
        latest = (
            db.query(NotifiedMatch.api_match_id)
            .filter(NotifiedMatch.home_score.isnot(None))
            .order_by(NotifiedMatch.api_match_id.desc())
            .first()
        )
        if not latest:
            await send_message(bot_token, chat_id, "No finished matches yet — stay tuned! ⏳")
            return

        match_id = latest[0]
        records: list[NotifiedMatch] = db.query(NotifiedMatch).filter_by(api_match_id=match_id).all()
        users: list[User] = db.query(User).all()
    finally:
        db.close()

    # Grab match details from any record
    ref = records[0]
    score = f"{ref.home_score}–{ref.away_score}"
    suffix = {"EXTRA_TIME": " aet", "PENALTY_SHOOTOUT": " pens"}.get(ref.duration or "", "")

    kickoff_str = "?"
    if user:
        match_home_en = normalize(spanish_to_english(ref.home_team or ""))
        match_away_en = normalize(spanish_to_english(ref.away_team or ""))
        match_pred = _find_prediction_by_match(user, match_home_en, match_away_en)
        if match_pred is not None:
            kickoff_iso = match_pred.get("kickoff_utc")
            if kickoff_iso:
                try:
                    dt = datetime.fromisoformat(kickoff_iso) + timedelta(hours=user.utc_offset_hours or 0.0)
                    kickoff_str = dt.strftime("%d %b %H:%M")
                except (ValueError, TypeError):
                    pass

    lines = [f"⚽ <b>Last match</b>", f"{kickoff_str} <b>{ref.home_team} {score}{suffix} {ref.away_team}</b>\n"]

    by_chat: dict[str, NotifiedMatch] = {r.telegram_chat_id: r for r in records}

    for u in sorted(users, key=lambda x: x.name):
        nm = by_chat.get(u.telegram_chat_id)
        if not nm:
            lines.append(f"⚪ <b>{u.name}</b>  › no prediction")
            continue
        mark = {2: "🎯", 1: "✅", 0: "❌"}.get(nm.correct, "⚪")
        if nm.prediction == "home":
            pred_label = ref.home_team
        elif nm.prediction == "away":
            pred_label = ref.away_team
        elif nm.prediction == "draw":
            pred_label = "draw"
        else:
            pred_label = nm.prediction
        if nm.predicted_home_goals is not None and nm.predicted_away_goals is not None:
            pred_label += f" ({nm.predicted_home_goals}–{nm.predicted_away_goals})"
        pts_str = f"  +{nm.points} pts" if nm.points is not None else ""
        lines.append(f"{mark} <b>{u.name}</b>  › {pred_label}{pts_str}")

    await send_message(bot_token, chat_id, "\n".join(lines))


async def _handle_next(chat_id: str, bot_token: str):
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
        nm_lookup = _build_nm_lookup(notified)
        preds = user.predictions or {}
        sorted_preds = sorted(((int(k), v) for k, v in preds.items()), key=lambda x: _kickoff_dt(x[1]))
        utc_offset = user.utc_offset_hours or 0.0

        next_pred = None
        for _match_num, pred in sorted_preds:
            home_en = normalize(spanish_to_english(pred.get("home_team", "")))
            away_en = normalize(spanish_to_english(pred.get("away_team", "")))
            nm = nm_lookup.get((home_en, away_en))
            if nm is None or nm.home_score is None:
                next_pred = pred
                break

        if not next_pred:
            await send_message(bot_token, chat_id, "✅ All matches are finished — no next match available.")
            return

        kickoff_str = "?"
        kickoff_iso = next_pred.get("kickoff_utc")
        if kickoff_iso:
            try:
                dt = datetime.fromisoformat(kickoff_iso) + timedelta(hours=utc_offset)
                kickoff_str = dt.strftime("%d %b %H:%M")
            except (ValueError, TypeError):
                pass

        home_es = next_pred.get("home_team", "?")
        away_es = next_pred.get("away_team", "?")
        prediction = next_pred.get("prediction") or "—"
        pred_hg = next_pred.get("predicted_home_goals")
        pred_ag = next_pred.get("predicted_away_goals")
        pred_label = _prediction_label(prediction, home_es, away_es, pred_hg, pred_ag)

        users: list[User] = db.query(User).order_by(User.name).all()
        match_home_en = normalize(spanish_to_english(home_es))
        match_away_en = normalize(spanish_to_english(away_es))

        lines = [
            f"⏭️ <b>Next match</b>",
            f"{kickoff_str} <b>{home_es} vs {away_es}</b>\n",
        ]

        for other in users:
            other_match = _find_prediction_by_match(other, match_home_en, match_away_en)
            if other_match is None:
                lines.append(f"⚪ <b>{other.name}</b>  › no prediction")
                continue

            other_prediction = other_match.get("prediction") or "—"
            other_pred_hg = other_match.get("predicted_home_goals")
            other_pred_ag = other_match.get("predicted_away_goals")
            other_label = _prediction_label(
                other_prediction,
                other_match.get("home_team", "?"),
                other_match.get("away_team", "?"),
                other_pred_hg,
                other_pred_ag,
            )
            lines.append(f"⚪ <b>{other.name}</b>  › {other_label}")

        await send_message(bot_token, chat_id, "\n".join(lines))
    finally:
        db.close()


async def _handle_delete(chat_id: str, bot_token: str, admin_chat_id: str, args: str):
    if chat_id != admin_chat_id:
        await send_message(bot_token, chat_id, "⛔ This command is only available to the admin.")
        return
    target = args.strip()
    if not target:
        await send_message(bot_token, chat_id, "Usage: /delete &lt;chat_id&gt;")
        return
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_chat_id=target).first()
        if not user:
            await send_message(bot_token, chat_id, f"❌ No user found with chat ID <code>{target}</code>.")
            return
        name = user.name
        nm_count = db.query(NotifiedMatch).filter_by(telegram_chat_id=target).delete()
        db.delete(user)
        db.commit()
    finally:
        db.close()
    await send_message(bot_token, chat_id, f"🗑 Deleted user <b>{name}</b> (<code>{target}</code>) and {nm_count} match record(s).")


async def _handle_update(update: dict, bot_token: str, admin_chat_id: str):
    message = update.get("message", {})
    text = (message.get("text") or "").strip()
    chat_id = str(message.get("chat", {}).get("id", ""))

    if not text.startswith("/") or not chat_id:
        return

    parts = text.split("@")[0].split()
    cmd = parts[0]
    args = " ".join(parts[1:])

    if cmd == "/status":
        await _handle_status(chat_id, bot_token)
    elif cmd == "/predictions":
        await _handle_predictions(chat_id, bot_token)
    elif cmd == "/results":
        await _handle_results(chat_id, bot_token)
    elif cmd == "/rank":
        await _handle_standings(chat_id, bot_token)
    elif cmd == "/last":
        await _handle_last(chat_id, bot_token)
    elif cmd == "/next":
        await _handle_next(chat_id, bot_token)
    elif cmd == "/users":
        await _handle_users(chat_id, bot_token, admin_chat_id)
    elif cmd == "/delete":
        await _handle_delete(chat_id, bot_token, admin_chat_id, args)
    elif cmd == "/chatid":
        await send_message(bot_token, chat_id, f"Your Telegram chat ID is: <code>{chat_id}</code>")


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

    for msg in messages:
        await send_message(bot_token, chat_id, msg)


async def polling_loop(bot_token: str):
    """Long-poll Telegram for bot commands. Runs as a background asyncio task."""
    from .config import get_settings
    admin_chat_id = get_settings().admin_telegram_chat_id
    logger.info("Telegram bot polling started")
    offset = 0
    while True:
        try:
            updates = await _get_updates(bot_token, offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                await _handle_update(upd, bot_token, admin_chat_id)
        except asyncio.CancelledError:
            logger.info("Telegram bot polling stopped")
            break
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)
