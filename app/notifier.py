import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from .football_api import APIMatch
from .team_names import names_match

logger = logging.getLogger(__name__)

_TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def is_knockout_prediction(prediction: Optional[str]) -> bool:
    """A knockout prediction is a team name (not a group-stage 1X2 outcome or empty)."""
    return prediction is not None and prediction not in ("home", "away", "draw")


def _actual_sign(match: APIMatch) -> Optional[str]:
    """1X2 of the actual result (the pre-penalty score for shootout matches)."""
    if match.home_score is None or match.away_score is None:
        return None
    if match.home_score > match.away_score:
        return "home"
    if match.home_score < match.away_score:
        return "away"
    return "draw"


def _predicted_sign(prediction: str, pred_home: Optional[int], pred_away: Optional[int]) -> Optional[str]:
    """1X2 implied by a prediction. Group stage stores it directly; knockout derives it
    from the predicted scoreline (so a predicted draw can be right even if the team that
    advanced on penalties was predicted wrong)."""
    if prediction in ("home", "away", "draw"):
        return prediction
    if pred_home is None or pred_away is None:
        return None
    if pred_home > pred_away:
        return "home"
    if pred_home < pred_away:
        return "away"
    return "draw"


def is_runner_up(prediction: str, match: APIMatch) -> bool:
    """Return True if prediction matches the LOSER of a knockout match."""
    if match.winner == "HOME_TEAM":
        loser_en = match.away_team
    elif match.winner == "AWAY_TEAM":
        loser_en = match.home_team
    else:
        return False
    return names_match(prediction, loser_en)


def advancement_earned(prediction: str, match: APIMatch) -> bool:
    """Return True if a knockout prediction (team name) matches the team that advanced."""
    if match.winner == "HOME_TEAM":
        winner_en = match.home_team
    elif match.winner == "AWAY_TEAM":
        winner_en = match.away_team
    else:
        return False
    return names_match(prediction, winner_en)


def is_exact_score(pred_home: Optional[int], pred_away: Optional[int], match: APIMatch) -> bool:
    if pred_home is None or pred_away is None:
        return False
    return pred_home == match.home_score and pred_away == match.away_score


@dataclass
class PointsBreakdown:
    score_points: int        # from the predicted scoreline (sign / goal diff / exact)
    advancement_points: int  # from the advancing-team pick (or runner-up bonus)
    score_cv: int            # scoreline accuracy: 0 wrong, 1 correct result, 2 exact
    advanced: bool           # predicted team advanced (knockout)
    runner_up: bool          # predicted the losing finalist (runner-up bonus)

    @property
    def total(self) -> int:
        return self.score_points + self.advancement_points


def points_breakdown(
    prediction: Optional[str],
    match: APIMatch,
    predicted_home_goals: Optional[int],
    predicted_away_goals: Optional[int],
    points_sign: int,
    points_goal_diff: int,
    points_exact: int,
    points_advancement: int = 0,
    points_runner_up: int = 0,
) -> PointsBreakdown:
    """Split a prediction's points into the scoreline component (all stages) and the
    advancing-team component (knockout only). The two are scored independently, so a correct
    scoreline still earns points even when the advancing-team pick is wrong (e.g. a draw
    decided on penalties)."""
    # Scoreline component
    score_points = 0
    score_cv = 0
    actual_sign = _actual_sign(match)
    predicted_sign = _predicted_sign(prediction, predicted_home_goals, predicted_away_goals)
    if actual_sign is not None and predicted_sign is not None and actual_sign == predicted_sign:
        score_points = points_sign
        score_cv = 1
        if (predicted_home_goals is not None and predicted_away_goals is not None
                and match.home_score is not None and match.away_score is not None):
            if predicted_home_goals - predicted_away_goals == match.home_score - match.away_score:
                score_points += points_goal_diff
                if predicted_home_goals == match.home_score and predicted_away_goals == match.away_score:
                    score_points += points_exact
                    score_cv = 2

    # Advancing-team component (knockout only)
    advancement_points = 0
    advanced = False
    runner_up = False
    if is_knockout_prediction(prediction):
        if advancement_earned(prediction, match):
            advanced = True
            advancement_points = points_advancement
        elif points_runner_up > 0 and is_runner_up(prediction, match):
            runner_up = True
            advancement_points = points_runner_up

    return PointsBreakdown(score_points, advancement_points, score_cv, advanced, runner_up)


def calculate_points(
    prediction: str,
    match: APIMatch,
    predicted_home_goals: Optional[int],
    predicted_away_goals: Optional[int],
    points_sign: int,
    points_goal_diff: int,
    points_exact: int,
    points_advancement: int = 0,
    points_runner_up: int = 0,
) -> int:
    """Total points earned for this match prediction."""
    return points_breakdown(
        prediction, match, predicted_home_goals, predicted_away_goals,
        points_sign, points_goal_diff, points_exact, points_advancement, points_runner_up,
    ).total


def correct_value(
    prediction: str,
    match: APIMatch,
    predicted_home_goals: Optional[int],
    predicted_away_goals: Optional[int],
    points_runner_up: int = 0,
) -> int:
    """Stored `correct` value: 2 = exact score, 1 = correct result, 3 = runner-up bonus, 0 = wrong.
    Reflects scoreline accuracy (the advancing-team point is tracked separately in `points`)."""
    bd = points_breakdown(
        prediction, match, predicted_home_goals, predicted_away_goals,
        1, 1, 1, 0, points_runner_up,
    )
    if bd.score_cv:
        return bd.score_cv
    if bd.runner_up:
        return 3
    return 0


def _result_line(match: APIMatch) -> str:
    line = f"{match.home_team} {match.home_score}–{match.away_score} {match.away_team}"
    if match.duration and match.duration != "REGULAR":
        line += {"EXTRA_TIME": " (a.e.t.)", "PENALTY_SHOOTOUT": " (pens.)"}.get(match.duration, "")
    return line


def _score_verdict(bd: PointsBreakdown) -> str:
    if bd.score_cv == 2:
        return f"🎯 Exact score!  <b>+{bd.score_points} pts</b>"
    if bd.score_cv == 1:
        return f"✅ Correct result  <b>+{bd.score_points} pts</b>"
    return "❌ Wrong score  +0 pts"


def _advancing_verdict(prediction: str, bd: PointsBreakdown) -> str:
    if bd.advanced:
        return f"✅ {prediction}  <b>+{bd.advancement_points} pts</b>"
    if bd.runner_up:
        return f"🥈 {prediction} (runner-up)  <b>+{bd.advancement_points} pts</b>"
    return f"❌ {prediction}  +0 pts"


def _group_pred_text(prediction: str, match: APIMatch,
                     predicted_home_goals: Optional[int], predicted_away_goals: Optional[int]) -> str:
    if prediction == "home":
        text = f"{match.home_team} win"
    elif prediction == "away":
        text = f"{match.away_team} win"
    else:
        text = "Draw"
    if predicted_home_goals is not None and predicted_away_goals is not None:
        text += f" ({predicted_home_goals}–{predicted_away_goals})"
    return text


def _knockout_pred_text(prediction: str,
                        predicted_home_goals: Optional[int], predicted_away_goals: Optional[int]) -> str:
    text = prediction
    if predicted_home_goals is not None and predicted_away_goals is not None:
        text += f" ({predicted_home_goals}–{predicted_away_goals})"
    return text


def build_message(
    match: APIMatch,
    prediction: Optional[str],
    predicted_home_goals: Optional[int],
    predicted_away_goals: Optional[int],
    breakdown: Optional[PointsBreakdown],
    total_points: int,
) -> str:
    result_line = _result_line(match)

    if prediction is None:
        return (
            f"⚽ <b>Match finished</b>\n"
            f"<b>{result_line}</b>\n\n"
            f"You didn't predict this match.\n"
            f"No prediction ⚪  +0 pts  (total: {total_points} pts)"
        )

    bd = breakdown
    if is_knockout_prediction(prediction):
        return (
            f"⚽ <b>Match finished</b>\n"
            f"<b>{result_line}</b>\n\n"
            f"Your prediction: {_knockout_pred_text(prediction, predicted_home_goals, predicted_away_goals)}\n"
            f"Score: {_score_verdict(bd)}\n"
            f"Advancing team: {_advancing_verdict(prediction, bd)}\n"
            f"<b>+{bd.total} pts</b>  (total: {total_points} pts)"
        )

    # Group stage
    pred_text = _group_pred_text(prediction, match, predicted_home_goals, predicted_away_goals)
    if bd.score_cv == 2:
        verdict = f"Exact score! 🎯  <b>+{bd.score_points} pts</b>"
    elif bd.score_cv == 1:
        verdict = f"Correct result! ✅  <b>+{bd.score_points} pts</b>"
    else:
        verdict = f"Wrong prediction ❌  +0 pts"

    return (
        f"⚽ <b>Match finished</b>\n"
        f"<b>{result_line}</b>\n\n"
        f"Your prediction: {pred_text}\n"
        f"{verdict}  (total: {total_points} pts)"
    )


def build_correction_message(
    match: APIMatch,
    old_home: int,
    old_away: int,
    prediction: str,
    predicted_home_goals: Optional[int],
    predicted_away_goals: Optional[int],
    breakdown: PointsBreakdown,
    old_points: int,
    total_points: int,
    points_runner_up: int = 0,
) -> str:
    bd = breakdown
    result_line = _result_line(match)
    new_points = bd.total
    pts_change = f"{old_points} → {new_points}" if old_points != new_points else str(new_points)

    if is_knockout_prediction(prediction):
        return (
            f"⚠️ <b>Score correction</b>\n"
            f"<b>{result_line}</b> (was {old_home}–{old_away})\n\n"
            f"Your prediction: {_knockout_pred_text(prediction, predicted_home_goals, predicted_away_goals)}\n"
            f"Score: {_score_verdict(bd)}\n"
            f"Advancing team: {_advancing_verdict(prediction, bd)}\n"
            f"<b>+{new_points} pts</b>  (pts: {pts_change}, total: {total_points} pts)"
        )

    pred_text = _group_pred_text(prediction, match, predicted_home_goals, predicted_away_goals)
    if bd.score_cv == 2:
        verdict = f"Exact score! 🎯  <b>+{new_points} pts</b>"
    elif bd.score_cv == 1:
        verdict = f"Correct result! ✅  <b>+{new_points} pts</b>"
    else:
        verdict = f"Wrong prediction ❌  +0 pts"

    return (
        f"⚠️ <b>Score correction</b>\n"
        f"<b>{result_line}</b> (was {old_home}–{old_away})\n\n"
        f"Your prediction: {pred_text}\n"
        f"{verdict}  (pts: {pts_change}, total: {total_points} pts)"
    )


async def send_message(bot_token: str, chat_id: str, text: str) -> bool:
    url = _TELEGRAM_URL.format(token=bot_token)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            return r.status_code == 200
        except httpx.RequestError as e:
            logger.error(f"Telegram request failed for chat_id={chat_id}: {e}")
            return False
