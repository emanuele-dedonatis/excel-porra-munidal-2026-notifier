import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from .config import get_settings
from .database import SessionLocal
from .espn_api import fetch_score
from .football_api import APIMatch, get_finished_matches, get_standings
from .group_rankings import (
    build_group_ranking_message,
    compute_ranking_points,
    simulate_predicted_standings,
)
from .models import GroupRankingAward, MatchRecheck, NotifiedMatch, User
from .notifier import build_correction_message, build_message, calculate_points, correct_value, send_message
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
        recheck_scheduled: set[int] = set()

        for match in finished:
            if match.home_score is None or match.away_score is None:
                espn = await fetch_score(match.home_team, match.away_team, match.kickoff_utc)
                if espn is None:
                    continue
                match.home_score, match.away_score = espn
                logger.info(f"ESPN fallback score for {match.home_team} vs {match.away_team}: {espn[0]}–{espn[1]}")

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

                sign_pts, goal_diff_pts, exact_pts = settings.stage_points(match.stage)
                pts = calculate_points(
                    pred["prediction"], match,
                    pred.get("predicted_home_goals"),
                    pred.get("predicted_away_goals"),
                    sign_pts, goal_diff_pts, exact_pts,
                )
                existing_pts = db.query(sa_func.sum(NotifiedMatch.points)).filter_by(
                    telegram_chat_id=user.telegram_chat_id,
                ).scalar() or 0

                text = build_message(
                    match=match,
                    prediction=pred["prediction"],
                    predicted_home_goals=pred.get("predicted_home_goals"),
                    predicted_away_goals=pred.get("predicted_away_goals"),
                    points=pts,
                    total_points=existing_pts + pts,
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
                        winner=match.winner,
                        stage=match.stage,
                        prediction=pred["prediction"],
                        predicted_home_goals=pred.get("predicted_home_goals"),
                        predicted_away_goals=pred.get("predicted_away_goals"),
                        correct=cv,
                        points=pts,
                    ))
                    if match.id not in recheck_scheduled and db.get(MatchRecheck, match.id) is None:
                        delay = timedelta(seconds=settings.score_recheck_delay_seconds)
                        db.add(MatchRecheck(
                            api_match_id=match.id,
                            recheck_after=(datetime.now(timezone.utc) + delay).replace(tzinfo=None),
                            notified_home=match.home_score,
                            notified_away=match.away_score,
                            done=False,
                        ))
                        recheck_scheduled.add(match.id)
                    db.commit()
                    logger.info(f"Notified {user.name} for match {match.home_team} vs {match.away_team}")
    finally:
        db.close()


async def _correct_match_scores(db: Session, match: APIMatch, settings) -> int:
    """Send correction messages to users whose stored score for match differs from the API score.
    Updates NotifiedMatch records in-place. Caller must commit. Returns count of corrected users."""
    notified_rows = db.query(NotifiedMatch).filter_by(api_match_id=match.id).all()
    corrected = 0
    for nm in notified_rows:
        if nm.home_score == match.home_score and nm.away_score == match.away_score:
            continue
        if nm.prediction is None:
            continue
        old_home = nm.home_score or 0
        old_away = nm.away_score or 0
        sign_pts, goal_diff_pts, exact_pts = settings.stage_points(match.stage)
        new_pts = calculate_points(
            nm.prediction, match,
            nm.predicted_home_goals, nm.predicted_away_goals,
            sign_pts, goal_diff_pts, exact_pts,
        )
        new_cv = correct_value(nm.prediction, match, nm.predicted_home_goals, nm.predicted_away_goals)
        current_total = db.query(sa_func.sum(NotifiedMatch.points)).filter_by(
            telegram_chat_id=nm.telegram_chat_id,
        ).scalar() or 0
        updated_total = current_total - (nm.points or 0) + new_pts
        text = build_correction_message(
            match=match,
            old_home=old_home,
            old_away=old_away,
            prediction=nm.prediction,
            predicted_home_goals=nm.predicted_home_goals,
            predicted_away_goals=nm.predicted_away_goals,
            old_points=nm.points or 0,
            new_points=new_pts,
            total_points=updated_total,
        )
        sent = await send_message(settings.telegram_bot_token, nm.telegram_chat_id, text)
        if sent:
            nm.home_score = match.home_score
            nm.away_score = match.away_score
            nm.correct = new_cv
            nm.points = new_pts
            corrected += 1
    return corrected


async def _recheck_scores():
    settings = get_settings()
    if not settings.football_data_api_key or not settings.telegram_bot_token:
        return

    db: Session = SessionLocal()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        pending = db.query(MatchRecheck).filter(
            MatchRecheck.recheck_after <= now,
            MatchRecheck.done == False,  # noqa: E712
        ).all()

        if not pending:
            return

        try:
            finished = await get_finished_matches(settings.football_data_api_key)
        except Exception as e:
            logger.error(f"Failed to fetch matches for score recheck: {e}")
            return

        finished_by_id = {m.id: m for m in finished}

        for recheck in pending:
            match = finished_by_id.get(recheck.api_match_id)
            if match is None:
                logger.warning(f"Match {recheck.api_match_id} not found in FINISHED list during recheck")
                recheck.done = True
                continue

            if match.home_score is None or match.away_score is None:
                espn = await fetch_score(match.home_team, match.away_team, match.kickoff_utc)
                if espn is None:
                    recheck.done = True
                    continue
                match.home_score, match.away_score = espn

            corrected = await _correct_match_scores(db, match, settings)
            if corrected:
                logger.info(
                    f"Score correction for match {match.id} ({match.home_team} vs {match.away_team}): "
                    f"corrected {corrected} users"
                )
            recheck.done = True

        db.commit()
    finally:
        db.close()


async def force_recheck_all() -> tuple[int, list[tuple[str, str, str, int]]]:
    """Re-check scores for all matches that have NotifiedMatch records.
    Sends correction messages to affected users and updates their records.
    Returns (matches_checked, corrections) where each correction is
    (match_label, old_score, new_score, users_corrected)."""
    settings = get_settings()
    db: Session = SessionLocal()
    try:
        try:
            finished = await get_finished_matches(settings.football_data_api_key)
        except Exception as e:
            logger.error(f"Failed to fetch matches for force recheck: {e}")
            return 0, []

        finished_by_id = {m.id: m for m in finished}
        stored_ids = {row[0] for row in db.query(NotifiedMatch.api_match_id).distinct().all()}

        matches_checked = 0
        corrections: list[tuple[str, str, str, int]] = []

        for api_match_id in stored_ids:
            match = finished_by_id.get(api_match_id)
            if match is None:
                continue

            if match.home_score is None or match.away_score is None:
                espn = await fetch_score(match.home_team, match.away_team, match.kickoff_utc)
                if espn is None:
                    continue
                match.home_score, match.away_score = espn

            matches_checked += 1

            stale = db.query(NotifiedMatch).filter(
                NotifiedMatch.api_match_id == api_match_id,
                (NotifiedMatch.home_score != match.home_score) | (NotifiedMatch.away_score != match.away_score),
            ).first()

            if stale:
                old_score = f"{stale.home_score}–{stale.away_score}"
                new_score = f"{match.home_score}–{match.away_score}"
                corrected = await _correct_match_scores(db, match, settings)
                if corrected:
                    corrections.append((
                        f"{match.home_team} vs {match.away_team}",
                        old_score, new_score, corrected,
                    ))
                    logger.info(
                        f"Force recheck: corrected {corrected} users for "
                        f"{match.home_team} vs {match.away_team} ({old_score} → {new_score})"
                    )

            recheck = db.get(MatchRecheck, api_match_id)
            if recheck and not recheck.done:
                recheck.done = True

        db.commit()
        return matches_checked, corrections
    finally:
        db.close()


async def _check_group_rankings():
    """Award group position points once each group's 3 matchdays are complete."""
    settings = get_settings()
    if not settings.football_data_api_key or not settings.telegram_bot_token:
        return

    try:
        standings = await get_standings(settings.football_data_api_key)
    except Exception as e:
        logger.error(f"Failed to fetch standings for group ranking check: {e}")
        return

    complete_groups = [g for g in standings if g["played"] >= 3]
    if not complete_groups:
        return

    db: Session = SessionLocal()
    try:
        users: list[User] = db.query(User).all()
        for group in complete_groups:
            group_name = group["group"]
            actual_pos = group["teams"]  # ordered 1→4 by API

            for user in users:
                already = db.query(GroupRankingAward).filter_by(
                    telegram_chat_id=user.telegram_chat_id,
                    group_name=group_name,
                ).first()
                if already:
                    continue

                if not user.predictions:
                    continue

                predicted_pos = simulate_predicted_standings(user.predictions, actual_pos)
                if predicted_pos is None:
                    logger.warning(
                        f"Could not simulate group standings for {user.name} / {group_name} "
                        f"(fewer than 6 predictions found)"
                    )
                    continue

                pts = compute_ranking_points(
                    predicted_pos, actual_pos, settings.points_group_rank_position
                )

                match_pts = db.query(sa_func.sum(NotifiedMatch.points)).filter_by(
                    telegram_chat_id=user.telegram_chat_id,
                ).scalar() or 0
                ranking_pts = db.query(sa_func.sum(GroupRankingAward.points)).filter_by(
                    telegram_chat_id=user.telegram_chat_id,
                ).scalar() or 0
                total_pts = match_pts + ranking_pts + pts

                text = build_group_ranking_message(group_name, predicted_pos, actual_pos, pts, total_pts)
                sent = await send_message(settings.telegram_bot_token, user.telegram_chat_id, text)
                if sent:
                    db.add(GroupRankingAward(
                        telegram_chat_id=user.telegram_chat_id,
                        group_name=group_name,
                        pred_pos=predicted_pos,
                        actual_pos=actual_pos,
                        points=pts,
                    ))
                    db.commit()
                    logger.info(
                        f"Group ranking awarded: {user.name} / {group_name} → {pts} pts "
                        f"(predicted {predicted_pos}, actual {actual_pos})"
                    )
    finally:
        db.close()


async def _check_matches():
    await _recheck_scores()
    await _process_finished_matches()
    await _check_group_rankings()


async def _daily_recheck():
    settings = get_settings()
    if not settings.football_data_api_key or not settings.telegram_bot_token:
        return

    matches_checked, corrections = await force_recheck_all()

    if not corrections:
        logger.info(f"Daily recheck: {matches_checked} matches checked, all scores correct")
        return

    logger.info(f"Daily recheck: {len(corrections)} correction(s) found across {matches_checked} matches")
    if settings.admin_telegram_chat_id:
        lines = [f"🔄 Daily recheck — {matches_checked} match(es) checked\n"]
        for match_label, old_score, new_score, user_count in corrections:
            lines.append(f"✏️ <b>{match_label}</b>: {old_score} → {new_score}  ({user_count} user(s) corrected)")
        await send_message(settings.telegram_bot_token, settings.admin_telegram_chat_id, "\n".join(lines))


async def seed_past_matches(telegram_chat_id: str):
    """
    Mark all already-finished matches as notified for a newly registered user,
    so they don't receive a flood of notifications for past games.
    Reads match results from the local DB to avoid hitting the API rate limit;
    falls back to the API only if no finished matches are stored yet.
    """
    settings = get_settings()
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_chat_id=telegram_chat_id).first()

        # Collect one stored record per api_match_id (from any other user)
        seen_ids: set[int] = set()
        stored: list[NotifiedMatch] = []
        for nm in db.query(NotifiedMatch).filter(NotifiedMatch.home_score.isnot(None)).all():
            if nm.api_match_id not in seen_ids:
                seen_ids.add(nm.api_match_id)
                stored.append(nm)

        if stored:
            finished_matches = [
                APIMatch(
                    id=nm.api_match_id,
                    home_team=nm.home_team,
                    away_team=nm.away_team,
                    kickoff_utc=datetime.now(timezone.utc),  # not used for scoring
                    status="FINISHED",
                    home_score=nm.home_score,
                    away_score=nm.away_score,
                    winner=nm.winner,
                    duration=nm.duration,
                    stage=nm.stage or "GROUP_STAGE",
                    matchday=None,
                )
                for nm in stored
            ]
        else:
            # No stored matches yet — fall back to API (first registration only)
            if not settings.football_data_api_key:
                return
            try:
                finished_matches = await get_finished_matches(settings.football_data_api_key)
            except Exception as e:
                logger.error(f"Failed to seed past matches for {telegram_chat_id}: {e}")
                return

        for match in finished_matches:
            exists = db.query(NotifiedMatch).filter_by(
                telegram_chat_id=telegram_chat_id, api_match_id=match.id
            ).first()
            if exists:
                continue
            pred = _find_prediction(user, match) if user else None
            sign_pts, goal_diff_pts, exact_pts = settings.stage_points(match.stage)
            entry = NotifiedMatch(
                telegram_chat_id=telegram_chat_id,
                api_match_id=match.id,
                home_team=match.home_team,
                away_team=match.away_team,
                home_score=match.home_score,
                away_score=match.away_score,
                duration=match.duration,
                winner=match.winner,
                stage=match.stage,
            )
            if pred:
                cv = correct_value(pred["prediction"], match, pred.get("predicted_home_goals"), pred.get("predicted_away_goals"))
                pts = calculate_points(pred["prediction"], match, pred.get("predicted_home_goals"), pred.get("predicted_away_goals"), sign_pts, goal_diff_pts, exact_pts)
                entry.prediction = pred["prediction"]
                entry.predicted_home_goals = pred.get("predicted_home_goals")
                entry.predicted_away_goals = pred.get("predicted_away_goals")
                entry.correct = cv
                entry.points = pts
            db.add(entry)
        db.commit()
    finally:
        db.close()


def start_scheduler():
    settings = get_settings()
    scheduler.add_job(
        _check_matches,
        "interval",
        seconds=settings.poll_interval_seconds,
        id="check_matches",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.add_job(
        _daily_recheck,
        "cron",
        hour=6,
        minute=0,
        id="daily_recheck",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started — polling every {settings.poll_interval_seconds}s, daily recheck at 06:00 UTC")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
