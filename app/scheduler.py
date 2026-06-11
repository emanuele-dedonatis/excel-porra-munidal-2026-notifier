import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from .config import get_settings
from .database import SessionLocal
from .football_api import APIMatch, get_finished_matches
from .models import NotifiedMatch, User
from .notifier import build_message, correct_value, send_message
from .team_names import names_match

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

_KICKOFF_TOLERANCE = timedelta(hours=1)


def _find_prediction(user: User, match: APIMatch) -> Optional[dict]:
    """
    Find the user's prediction for an API match.
    Primary strategy: team name matching (Spanish → English via mapping).
    Fallback: closest kickoff time within tolerance.
    """
    if not user.predictions:
        return None

    # Primary: exact team name match
    for pred in user.predictions.values():
        home_es = pred.get("home_team", "")
        away_es = pred.get("away_team", "")
        if names_match(home_es, match.home_team) and names_match(away_es, match.away_team):
            return pred

    # Fallback: closest kickoff within tolerance (handles unmapped team names)
    best_pred = None
    best_diff = _KICKOFF_TOLERANCE
    for pred in user.predictions.values():
        kickoff_iso = pred.get("kickoff_utc")
        if not kickoff_iso:
            continue
        try:
            pred_kickoff = datetime.fromisoformat(kickoff_iso)
            if pred_kickoff.tzinfo is None:
                pred_kickoff = pred_kickoff.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        diff = abs(pred_kickoff - match.kickoff_utc)
        if diff < best_diff:
            best_diff = diff
            best_pred = pred

    return best_pred


async def _process_finished_matches():
    settings = get_settings()
    if not settings.football_data_api_key or not settings.telegram_bot_token:
        logger.warning("Missing API key or bot token — skipping match check")
        return

    try:
        finished = await get_finished_matches(settings.football_data_api_key)
    except Exception as e:
        logger.error(f"Failed to fetch matches: {e}")
        return

    if not finished:
        return

    db: Session = SessionLocal()
    try:
        users: list[User] = db.query(User).all()

        for match in finished:
            if match.home_score is None or match.away_score is None:
                continue

            for user in users:
                already = db.query(NotifiedMatch).filter_by(
                    telegram_chat_id=user.telegram_chat_id,
                    api_match_id=match.id,
                ).first()
                if already:
                    continue

                pred = _find_prediction(user, match)
                if pred is None:
                    continue

                text = build_message(
                    match=match,
                    prediction=pred["prediction"],
                    predicted_home_goals=pred.get("predicted_home_goals"),
                    predicted_away_goals=pred.get("predicted_away_goals"),
                )
                sent = await send_message(settings.telegram_bot_token, user.telegram_chat_id, text)
                if sent:
                    cv = correct_value(
                        pred["prediction"], match,
                        pred.get("predicted_home_goals"),
                        pred.get("predicted_away_goals"),
                    )
                    db.add(NotifiedMatch(
                        telegram_chat_id=user.telegram_chat_id,
                        api_match_id=match.id,
                        home_team=match.home_team,
                        away_team=match.away_team,
                        home_score=match.home_score,
                        away_score=match.away_score,
                        duration=match.duration,
                        prediction=pred["prediction"],
                        predicted_home_goals=pred.get("predicted_home_goals"),
                        predicted_away_goals=pred.get("predicted_away_goals"),
                        correct=cv,
                    ))
                    db.commit()
                    logger.info(f"Notified {user.name} for match {match.home_team} vs {match.away_team}")
    finally:
        db.close()


async def seed_past_matches(telegram_chat_id: str):
    """
    Mark all already-finished matches as notified for a newly registered user,
    so they don't receive a flood of notifications for past games.
    Stores full match info so /status shows the correct history immediately.
    """
    settings = get_settings()
    if not settings.football_data_api_key:
        return
    try:
        finished = await get_finished_matches(settings.football_data_api_key)
    except Exception as e:
        logger.error(f"Failed to seed past matches for {telegram_chat_id}: {e}")
        return

    db: Session = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_chat_id=telegram_chat_id).first()
        for match in finished:
            exists = db.query(NotifiedMatch).filter_by(
                telegram_chat_id=telegram_chat_id, api_match_id=match.id
            ).first()
            if exists:
                continue
            pred = _find_prediction(user, match) if user else None
            entry = NotifiedMatch(
                telegram_chat_id=telegram_chat_id,
                api_match_id=match.id,
                home_team=match.home_team,
                away_team=match.away_team,
                home_score=match.home_score,
                away_score=match.away_score,
                duration=match.duration,
            )
            if pred:
                entry.prediction = pred["prediction"]
                entry.predicted_home_goals = pred.get("predicted_home_goals")
                entry.predicted_away_goals = pred.get("predicted_away_goals")
                entry.correct = correct_value(
                    pred["prediction"], match,
                    pred.get("predicted_home_goals"),
                    pred.get("predicted_away_goals"),
                )
            db.add(entry)
        db.commit()
    finally:
        db.close()


def start_scheduler():
    settings = get_settings()
    scheduler.add_job(
        _process_finished_matches,
        "interval",
        seconds=settings.poll_interval_seconds,
        id="check_matches",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    logger.info(f"Scheduler started — polling every {settings.poll_interval_seconds}s")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
