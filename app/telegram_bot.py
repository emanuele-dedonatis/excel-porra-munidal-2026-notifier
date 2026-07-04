import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from .config import get_settings
from .database import SessionLocal
from .football_api import get_matches
from .models import GroupRankingAward, NotifiedMatch, User
from .notifier import (STAGE_ROUND_LABEL, is_knockout_prediction, predicted_round_participant,
                       predicted_round_winner, send_message)
from .scheduler import _find_prediction, force_recheck_all, recalculate_r32_advancement
from .team_names import ENGLISH_TO_SPANISH, names_match, normalize, spanish_to_english

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


def _advancement_pts(n: NotifiedMatch) -> int:
    """Advancement component of a row's stored points (total minus the score component)."""
    total = n.points or 0
    if total <= 0:
        return 0
    settings = get_settings()
    sign_pts, goal_diff_pts, exact_pts = settings.stage_points(n.stage or "GROUP_STAGE")
    cv = n.correct or 0
    score = 0
    if cv in (1, 2):
        score = sign_pts
        if (n.predicted_home_goals is not None and n.predicted_away_goals is not None
                and n.home_score is not None and n.away_score is not None
                and (n.predicted_home_goals - n.predicted_away_goals) == (n.home_score - n.away_score)):
            score += goal_diff_pts
            if cv == 2:
                score += exact_pts
    return max(total - score, 0)


def _stats(matches: list[NotifiedMatch]) -> tuple[int, int, int, int, int, int]:
    """Return (total, sign_only, goal_diff, exact, advancement, total_pts) for a list of
    finished NotifiedMatches. advancement counts matches whose advancing-team bonus was earned."""
    total = sign_only = goal_diff = exact = advancement = total_pts = 0
    for n in matches:
        total += 1
        total_pts += n.points or 0
        if _advancement_pts(n) > 0:
            advancement += 1
        cv = n.correct or 0
        if cv not in (1, 2):
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
    return total, sign_only, goal_diff, exact, advancement, total_pts


def _stats_line(total: int, sign_only: int, goal_diff: int, exact: int, advancement: int) -> str:
    wrong = total - sign_only - goal_diff - exact
    return (f"✅ {sign_only + goal_diff + exact}  ⚽ {goal_diff + exact}  🎯 {exact}  "
            f"❌ {wrong}  🎫 {advancement}")


def _group_ranking_pts(db: Session, telegram_chat_id: str) -> int:
    pos_pts = db.query(sa_func.sum(GroupRankingAward.points)).filter_by(
        telegram_chat_id=telegram_chat_id,
    ).scalar() or 0
    adv_pts = db.query(sa_func.sum(GroupRankingAward.advancement_points)).filter_by(
        telegram_chat_id=telegram_chat_id,
    ).scalar() or 0
    return pos_pts + adv_pts


def _build_status(user: User, notified: list[NotifiedMatch], group_rank_pts: int = 0) -> str:
    finished = [n for n in notified if n.home_team and n.away_team]

    lines = [f"📊 <b>WC 2026 — {user.name}</b>"]

    if not finished:
        lines.append("\nNo matches finished yet — stay tuned! ⏳")
        return "\n".join(lines)

    total_f = len(finished)
    _, sign_only, goal_diff, exact, advancement, match_pts = _stats(finished)
    total_pts = match_pts + group_rank_pts
    lines.append(_stats_line(total_f, sign_only, goal_diff, exact, advancement))
    lines.append(f"⭐ Total points: <b>{total_pts} pts</b>")

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
        grp_pts = _group_ranking_pts(db, chat_id)
        text = _build_status(user, notified, grp_pts)
    finally:
        db.close()

    await send_message(bot_token, chat_id, text)


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


def _round_advancement_winners(notified: list[NotifiedMatch]) -> dict[str, list[tuple[str, int]]]:
    """round_label → [(advancing team EN, advancement pts this user earned)] for
    finished knockout matches. Used to credit 🎫 on predicted pairings that never
    took place but whose advancing-team pick still came true."""
    winners: dict[str, list[tuple[str, int]]] = {}
    for nm in notified:
        label = STAGE_ROUND_LABEL.get(nm.stage or "")
        if not label or nm.home_score is None:
            continue
        winner_en = {"HOME_TEAM": nm.home_team, "AWAY_TEAM": nm.away_team}.get(nm.winner or "")
        if winner_en:
            winners.setdefault(label, []).append((winner_en, _advancement_pts(nm)))
    return winners


def _prediction_line(pred: dict, nm_lookup: dict, utc_offset: float = 0.0,
                     round_winners: dict[str, list[tuple[str, int]]] | None = None) -> str:
    home_es = pred.get("home_team", "?")
    away_es = pred.get("away_team", "?")
    prediction = pred.get("prediction") or "—"
    pred_hg = pred.get("predicted_home_goals")
    pred_ag = pred.get("predicted_away_goals")

    pred_label = _prediction_label(prediction, home_es, away_es, pred_hg, pred_ag)

    kickoff_str = "?"
    kickoff_dt = None
    kickoff_iso = pred.get("kickoff_utc")
    if kickoff_iso:
        try:
            kickoff_dt = datetime.fromisoformat(kickoff_iso)
            if kickoff_dt.tzinfo is None:
                kickoff_dt = kickoff_dt.replace(tzinfo=timezone.utc)
            kickoff_str = (kickoff_dt + timedelta(hours=utc_offset)).strftime("%d %b %H:%M")
        except (ValueError, TypeError):
            kickoff_dt = None

    # Look up finished match by mapped English team names (try both home/away orderings)
    home_en = normalize(spanish_to_english(home_es))
    away_en = normalize(spanish_to_english(away_es))
    nm = nm_lookup.get((home_en, away_en)) or nm_lookup.get((away_en, home_en))

    if nm and nm.home_score is not None:
        score = f"{nm.home_score}–{nm.away_score}"
        suffix = {"EXTRA_TIME": " aet", "PENALTY_SHOOTOUT": " pens"}.get(nm.duration or "", "")
        mark = {3: "🥈", 2: "🎯", 1: "✅", 0: "❌"}.get(nm.correct, "⚪")
        if mark == "❌" and _advancement_pts(nm) > 0:
            # Score wrong, but the advancing-team bonus was earned.
            mark = "🎫"
        pts_str = f"  +{nm.points} pts" if nm.points is not None else ""
        return f"{mark} {kickoff_str} {nm.home_team} {score}{suffix} {nm.away_team}  › {pred_label}{pts_str}"
    else:
        # Pending match: 🔮 prediction registered, ⚪ no pick in the Excel row.
        mark = "⚪" if prediction == "—" else "🔮"
        # A predicted matchup whose kickoff is well past but has no stored result
        # never took place (divergent knockout bracket) — it can't score anymore.
        # 🎫 if the advancing-team pick still came true in the round's real fixture.
        if kickoff_dt is not None and datetime.now(timezone.utc) - kickoff_dt > timedelta(hours=4):
            mark, pts_str = "❌", "  +0 pts"
            if is_knockout_prediction(prediction) and prediction != "—":
                label = str(pred.get("round_label", ""))
                for winner_en, adv_pts in (round_winners or {}).get(label, []):
                    if adv_pts > 0 and names_match(prediction, winner_en):
                        mark, pts_str = "🎫", f"  +{adv_pts} pts"
                        break
            return f"{mark} {kickoff_str} {home_es} vs {away_es}  › {pred_label}{pts_str}"
        return f"{mark} {kickoff_str} {home_es} vs {away_es}  › {pred_label}"


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


def _is_past_pred(pred: dict, nm_lookup: dict) -> bool:
    """A prediction row is 'past' when its fixture has a stored result, or its
    kickoff is more than 4h gone (matchup never took place, or result pending)."""
    home_en = normalize(spanish_to_english(pred.get("home_team", "")))
    away_en = normalize(spanish_to_english(pred.get("away_team", "")))
    nm = nm_lookup.get((home_en, away_en)) or nm_lookup.get((away_en, home_en))
    if nm is not None and nm.home_score is not None:
        return True
    kickoff_iso = pred.get("kickoff_utc")
    if not kickoff_iso:
        return False
    try:
        kickoff_dt = datetime.fromisoformat(kickoff_iso)
    except (ValueError, TypeError):
        return False
    if kickoff_dt.tzinfo is None:
        kickoff_dt = kickoff_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - kickoff_dt > timedelta(hours=4)


def _build_prediction_messages(user: User, notified: list[NotifiedMatch], past: bool) -> list[str]:
    """Per-round messages for the user's prediction rows: past=True yields the
    played/expired rows (/results), past=False the upcoming ones (/predictions)."""
    nm_lookup = _build_nm_lookup(notified)
    round_winners = _round_advancement_winners(notified)
    preds = user.predictions or {}
    sorted_preds = sorted(((int(k), v) for k, v in preds.items()), key=lambda x: _kickoff_dt(x[1]))
    utc_offset = user.utc_offset_hours or 0.0

    all_messages: list[str] = []

    for title, rng in [("🏟 <b>Group Stage</b>", range(1, 73)), *_KNOCKOUT_ROUNDS]:
        round_preds = [p for n, p in sorted_preds if n in rng and _is_past_pred(p, nm_lookup) == past]
        if not round_preds:
            continue
        lines = [title]
        for pred in round_preds:
            lines.append(_prediction_line(pred, nm_lookup, utc_offset, round_winners))
        all_messages.extend(_chunk_messages(lines))

    return all_messages


def _build_predictions(user: User, notified: list[NotifiedMatch]) -> list[str]:
    return (_build_prediction_messages(user, notified, past=False)
            or ["No upcoming matches left — the tournament is over! 🏁"])


def _build_results(user: User, notified: list[NotifiedMatch]) -> list[str]:
    return (_build_prediction_messages(user, notified, past=True)
            or ["No finished matches yet — stay tuned! ⏳"])


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
        match_pts_by_user = {
            cid: pts or 0
            for cid, pts in db.query(
                NotifiedMatch.telegram_chat_id,
                sa_func.sum(NotifiedMatch.points),
            ).group_by(NotifiedMatch.telegram_chat_id).all()
        }
        grp_pts_by_user = {
            cid: (pos_pts or 0) + (adv_pts or 0)
            for cid, pos_pts, adv_pts in db.query(
                GroupRankingAward.telegram_chat_id,
                sa_func.sum(GroupRankingAward.points),
                sa_func.sum(GroupRankingAward.advancement_points),
            ).group_by(GroupRankingAward.telegram_chat_id).all()
        }
    finally:
        db.close()

    if not users:
        await send_message(bot_token, chat_id, "No users registered yet.")
        return

    lines = [f"👥 <b>Registered users ({len(users)})</b>"]
    for u in users:
        pts = match_pts_by_user.get(u.telegram_chat_id, 0) + grp_pts_by_user.get(u.telegram_chat_id, 0)
        lines.append(f"• {u.name}  <code>{u.telegram_chat_id}</code>  ⭐ {pts} pts")

    await send_message(bot_token, chat_id, "\n".join(lines))


async def _handle_standings(chat_id: str, bot_token: str):
    db: Session = SessionLocal()
    try:
        users: list[User] = db.query(User).all()
        all_notified = db.query(NotifiedMatch).filter(NotifiedMatch.home_team.isnot(None)).all()
        grp_pts_by_user: dict[str, int] = {
            cid: (pos_pts or 0) + (adv_pts or 0)
            for cid, pos_pts, adv_pts in db.query(
                GroupRankingAward.telegram_chat_id,
                sa_func.sum(GroupRankingAward.points),
                sa_func.sum(GroupRankingAward.advancement_points),
            ).group_by(GroupRankingAward.telegram_chat_id).all()
        }
    finally:
        db.close()

    if not users:
        await send_message(bot_token, chat_id, "No users registered yet.")
        return

    by_user: dict[str, list[NotifiedMatch]] = {}
    for nm in all_notified:
        by_user.setdefault(nm.telegram_chat_id, []).append(nm)

    def sort_key(u: User):
        _, _, _, _, _, match_pts = _stats(by_user.get(u.telegram_chat_id, []))
        return match_pts + grp_pts_by_user.get(u.telegram_chat_id, 0)

    ranked = sorted(users, key=sort_key, reverse=True)

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 <b>Standings</b>\n"]
    for i, u in enumerate(ranked, 1):
        total, sign_only, goal_diff, exact, advancement, match_pts = _stats(by_user.get(u.telegram_chat_id, []))
        grp_pts = grp_pts_by_user.get(u.telegram_chat_id, 0)
        total_pts = match_pts + grp_pts
        prefix = medals.get(i, f"{i}.")
        lines.append(f"{prefix} <b>{u.name}</b>  ⭐ {total_pts} pts\n    {_stats_line(total, sign_only, goal_diff, exact, advancement)}")

    await send_message(bot_token, chat_id, "\n".join(lines))


async def _handle_last(chat_id: str, bot_token: str):
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_chat_id=chat_id).first()
        if not user:
            await send_message(bot_token, chat_id,
                               "⚠️ You're not registered yet.\nVisit the app to upload your Excel predictions.")
            return

        latest = (
            db.query(NotifiedMatch.api_match_id)
            .filter(NotifiedMatch.home_score.isnot(None))
            .order_by(NotifiedMatch.notified_at.desc())
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

    await send_message(bot_token, chat_id, _build_last_message(records, users))


def _build_last_message(records: list[NotifiedMatch], users: list[User]) -> str:
    # Grab match details from any record
    ref = records[0]
    score = f"{ref.home_score}–{ref.away_score}"
    suffix = {"EXTRA_TIME": " aet", "PENALTY_SHOOTOUT": " pens"}.get(ref.duration or "", "")

    lines = [f"⚽ <b>Last match</b>", f"<b>{ref.home_team} {score}{suffix} {ref.away_team}</b>"]

    by_chat: dict[str, NotifiedMatch] = {r.telegram_chat_id: r for r in records}

    # The advancement line only applies to knockout fixtures; the Final's
    # champion/runner-up bonuses are shown in the notification itself, not here.
    show_advancement = (ref.stage or "GROUP_STAGE") not in ("GROUP_STAGE", "FINAL")

    def _pts(n: int) -> str:
        return f"+{n} pt" if n == 1 else f"+{n} pts"

    for u in sorted(users, key=lambda x: x.name):
        nm = by_chat.get(u.telegram_chat_id)
        adv_pts = _advancement_pts(nm) if nm else 0
        score_pts = max((nm.points or 0) - adv_pts, 0) if nm else 0

        lines.append(f"\n<b>{u.name}</b>")

        if nm is None or nm.prediction is None:
            lines.append("❌ Score not predicted  +0 pts")
        else:
            if nm.predicted_home_goals is not None and nm.predicted_away_goals is not None:
                pred_label = f"{nm.predicted_home_goals}–{nm.predicted_away_goals}"
            elif nm.prediction == "home":
                pred_label = f"{ref.home_team} win"
            elif nm.prediction == "away":
                pred_label = f"{ref.away_team} win"
            elif nm.prediction == "draw":
                pred_label = "draw"
            else:
                pred_label = nm.prediction
            mark = {2: "🎯", 1: "✅"}.get(nm.correct or 0, "❌")
            lines.append(f"{mark} Score {pred_label}  {_pts(score_pts)}")

        if show_advancement:
            if adv_pts > 0:
                lines.append(f"🎫 Advancement  {_pts(adv_pts)}")
            else:
                lines.append("❌ Advancement  +0 pts")

    return "\n".join(lines)


# Cache of the full fixtures list so repeated /next calls don't hit the API
# more than once per minute. The cached data still carries live scores.
_FIXTURES_CACHE_TTL = 60.0
_fixtures_cache: tuple[float, list] | None = None


async def _get_fixtures_cached() -> list:
    """Return tournament fixtures, refreshing from the API at most once per
    minute. On failure, fall back to the last cached value if available."""
    global _fixtures_cache
    now = time.monotonic()
    if _fixtures_cache and now - _fixtures_cache[0] < _FIXTURES_CACHE_TTL:
        return _fixtures_cache[1]
    try:
        matches = await get_matches(get_settings().football_data_api_key)
        _fixtures_cache = (now, matches)
        return matches
    except Exception:
        logger.exception("/next: failed to fetch fixtures")
        return _fixtures_cache[1] if _fixtures_cache else []


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

        utc_offset = user.utc_offset_hours or 0.0
        users: list[User] = db.query(User).order_by(User.name).all()

        # The "next match" must be the same for everyone and reflect the real
        # tournament schedule — not any single user's predictions (knockout
        # brackets diverge per user, and some uploads have corrupt kickoffs).
        # So we take the next real fixture from the football API and correlate
        # each user's prediction to it via the same logic used for scoring.
        api_matches = await _get_fixtures_cached()

        # Earliest non-finished match — this includes a match that's currently
        # being played (kickoff already passed), since that's the next result
        # to be scored.
        pending = sorted(
            (m for m in api_matches if m.status != "FINISHED"),
            key=lambda m: m.kickoff_utc,
        )
        next_match = pending[0] if pending else None

        if next_match is None:
            await send_message(bot_token, chat_id, "✅ All matches are finished — no next match available.")
            return

        ongoing = next_match.status in ("IN_PLAY", "PAUSED")
        kickoff_str = (next_match.kickoff_utc + timedelta(hours=utc_offset)).strftime("%d %b %H:%M")

        # Per-user predictions for this fixture (None if they didn't predict
        # this matchup — consistent with how scoring treats it).
        user_preds = {u.telegram_chat_id: _find_prediction(u, next_match) for u in users}

        # Header teams come from the API (real fixture), translated to Spanish
        # and kept in home/away order so a live score stays aligned.
        header_home = ENGLISH_TO_SPANISH.get(next_match.home_team, next_match.home_team)
        header_away = ENGLISH_TO_SPANISH.get(next_match.away_team, next_match.away_team)

        if ongoing:
            score = ""
            if next_match.home_score is not None and next_match.away_score is not None:
                score = f"  ⚽ {next_match.home_score}–{next_match.away_score}"
            lines = [
                f"🟢 <b>Ongoing match</b>",
                f"{kickoff_str} <b>{header_home} vs {header_away}</b>{score}\n",
            ]
        else:
            lines = [
                f"⏭️ <b>Next match</b>",
                f"{kickoff_str} <b>{header_home} vs {header_away}</b>\n",
            ]

        # Advancement picks (matchup-independent, same rules as scoring) are shown
        # for users without a pairing prediction — knockout rounds except the Final,
        # consistent with /last. SF winners are checked against the predicted Final.
        stage = next_match.stage or ""
        round_label = STAGE_ROUND_LABEL.get(stage)

        def _predicted_to_advance(preds: dict | None, team_en: str) -> bool:
            if not round_label or stage == "FINAL" or not preds:
                return False
            if stage == "SEMI_FINALS":
                return predicted_round_participant(preds, "Final", team_en)
            return predicted_round_winner(preds, round_label, team_en)

        for other in users:
            other_match = user_preds.get(other.telegram_chat_id)
            if other_match is None:
                picked = []
                if _predicted_to_advance(other.predictions, next_match.home_team):
                    picked.append(header_home)
                if _predicted_to_advance(other.predictions, next_match.away_team):
                    picked.append(header_away)
                if picked:
                    verb = "advance" if len(picked) > 1 else "advances"
                    lines.append(f"🎫 <b>{other.name}</b>  › {' & '.join(picked)} {verb}")
                else:
                    lines.append(f"⚪ <b>{other.name}</b>  › no prediction")
                continue

            other_label = _prediction_label(
                other_match.get("prediction") or "—",
                other_match.get("home_team", "?"),
                other_match.get("away_team", "?"),
                other_match.get("predicted_home_goals"),
                other_match.get("predicted_away_goals"),
            )
            lines.append(f"🔮 <b>{other.name}</b>  › {other_label}")

        await send_message(bot_token, chat_id, "\n".join(lines))
    finally:
        db.close()


async def _handle_recheck(chat_id: str, bot_token: str, admin_chat_id: str, dry_run: bool = False):
    if chat_id != admin_chat_id:
        await send_message(bot_token, chat_id, "⛔ This command is only available to the admin.")
        return

    mode_label = " (dry run — no changes will be made)" if dry_run else ""
    await send_message(bot_token, chat_id, f"⏳ Rechecking{mode_label}…")

    matches_checked, corrections = await force_recheck_all(dry_run=dry_run)
    verb = "would be corrected" if dry_run else "corrected"
    if not corrections:
        score_msg = f"✅ Match scores: {matches_checked} match(es) checked, all correct."
    else:
        header = "🔍 Match scores (dry run)" if dry_run else "🔄 Match scores"
        lines = [f"{header}: {matches_checked} match(es) checked\n"]
        for match_label, old_score, new_score, user_count in corrections:
            lines.append(f"✏️ <b>{match_label}</b>: {old_score} → {new_score}  ({user_count} user(s) {verb})")
        score_msg = "\n".join(lines)
    await send_message(bot_token, chat_id, score_msg)

    r32_summary = await recalculate_r32_advancement(dry_run=dry_run)
    await send_message(bot_token, chat_id, f"🌍 <b>R32 bonus check</b>\n{r32_summary}")


async def _handle_broadcast(chat_id: str, bot_token: str, admin_chat_id: str, args: str):
    if chat_id != admin_chat_id:
        await send_message(bot_token, chat_id, "⛔ This command is only available to the admin.")
        return
    text = args.strip()
    if not text:
        await send_message(bot_token, chat_id, "Usage: /broadcast &lt;message text&gt;")
        return
    db: Session = SessionLocal()
    try:
        users: list[User] = db.query(User).all()
    finally:
        db.close()
    if not users:
        await send_message(bot_token, chat_id, "No registered users to send to.")
        return
    full_text = f"📢 <b>Admin message:</b>\n\n{text}"
    sent = failed = 0
    for user in users:
        ok = await send_message(bot_token, user.telegram_chat_id, full_text)
        if ok:
            sent += 1
        else:
            failed += 1
    await send_message(bot_token, chat_id, f"📢 Broadcast sent: {sent} delivered, {failed} failed.")


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
    elif cmd == "/broadcast":
        await _handle_broadcast(chat_id, bot_token, admin_chat_id, args)
    elif cmd == "/delete":
        await _handle_delete(chat_id, bot_token, admin_chat_id, args)
    elif cmd == "/recheck":
        dry_run = args.strip().lower() in ("dry-run", "dry_run", "dryrun")
        await _handle_recheck(chat_id, bot_token, admin_chat_id, dry_run=dry_run)
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
