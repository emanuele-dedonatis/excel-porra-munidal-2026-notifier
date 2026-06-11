import logging
from typing import Optional

import httpx

from .football_api import APIMatch
from .team_names import names_match, normalize

logger = logging.getLogger(__name__)

_TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def is_correct(prediction: str, match: APIMatch) -> bool:
    """Return True if the user's prediction matches the actual result."""
    if prediction in ("home", "away", "draw"):
        if match.home_score is None or match.away_score is None:
            return False
        if match.home_score > match.away_score:
            actual = "home"
        elif match.home_score < match.away_score:
            actual = "away"
        else:
            actual = "draw"
        return prediction == actual

    # Knockout: prediction is a Spanish team name; winner comes from API score.winner
    if match.winner == "HOME_TEAM":
        winner_en = match.home_team
    elif match.winner == "AWAY_TEAM":
        winner_en = match.away_team
    else:
        return False

    return names_match(prediction, winner_en)


def is_exact_score(
    pred_home: Optional[int],
    pred_away: Optional[int],
    match: APIMatch,
) -> bool:
    if pred_home is None or pred_away is None:
        return False
    return pred_home == match.home_score and pred_away == match.away_score


def correct_value(
    prediction: str,
    match: APIMatch,
    predicted_home_goals: Optional[int],
    predicted_away_goals: Optional[int],
) -> int:
    """Return 2 for exact score, 1 for correct result, 0 for wrong."""
    if not is_correct(prediction, match):
        return 0
    return 2 if is_exact_score(predicted_home_goals, predicted_away_goals, match) else 1


def calculate_points(
    prediction: str,
    match: APIMatch,
    predicted_home_goals: Optional[int],
    predicted_away_goals: Optional[int],
    points_sign: int,
    points_goal_diff: int,
    points_exact: int,
) -> int:
    """Calculate points earned for this match prediction."""
    if not is_correct(prediction, match):
        return 0
    pts = points_sign
    if (predicted_home_goals is not None and predicted_away_goals is not None
            and match.home_score is not None and match.away_score is not None):
        pred_diff = predicted_home_goals - predicted_away_goals
        actual_diff = match.home_score - match.away_score
        if pred_diff == actual_diff:
            pts += points_goal_diff
            if predicted_home_goals == match.home_score and predicted_away_goals == match.away_score:
                pts += points_exact
    return pts


def build_message(
    match: APIMatch,
    prediction: str,
    predicted_home_goals: Optional[int],
    predicted_away_goals: Optional[int],
    points: int,
    total_points: int,
) -> str:
    cv = correct_value(prediction, match, predicted_home_goals, predicted_away_goals)
    correct = cv >= 1
    exact = cv == 2

    result_line = f"{match.home_team} {match.home_score}–{match.away_score} {match.away_team}"

    if match.duration and match.duration != "REGULAR":
        duration_note = {"EXTRA_TIME": " (a.e.t.)", "PENALTY_SHOOTOUT": " (pens.)"}.get(match.duration, "")
        result_line += duration_note

    if prediction == "home":
        pred_text = f"{match.home_team} win"
    elif prediction == "away":
        pred_text = f"{match.away_team} win"
    elif prediction == "draw":
        pred_text = "Draw"
    else:
        pred_text = f"{prediction} wins"

    if predicted_home_goals is not None and predicted_away_goals is not None:
        pred_text += f" ({predicted_home_goals}–{predicted_away_goals})"

    if exact:
        verdict = f"Exact score! 🎯  <b>+{points} pts</b>"
    elif correct:
        verdict = f"Correct result! ✅  <b>+{points} pts</b>"
    else:
        verdict = f"Wrong prediction ❌  +0 pts"

    return (
        f"⚽ <b>Match finished</b>\n"
        f"<b>{result_line}</b>\n\n"
        f"Your prediction: {pred_text}\n"
        f"{verdict}  (total: {total_points} pts)"
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
