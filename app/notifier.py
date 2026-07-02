import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from .football_api import APIMatch
from .team_names import names_match

logger = logging.getLogger(__name__)

_TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Round label used in the Excel template for each API knockout stage
STAGE_ROUND_LABEL = {
    "LAST_32": "1/32",
    "LAST_16": "1/16",
    "QUARTER_FINALS": "1/4",
    "SEMI_FINALS": "1/2",
    "THIRD_PLACE": "3/4",
    "FINAL": "Final",
}


def is_knockout_prediction(prediction: Optional[str]) -> bool:
    """A knockout prediction is a team name (not a group-stage 1X2 outcome or empty)."""
    return prediction is not None and prediction not in ("home", "away", "draw")


def winner_loser_teams(match: APIMatch) -> tuple[Optional[str], Optional[str]]:
    """(advancing team, eliminated team) in English, or (None, None) if undecided."""
    if match.winner == "HOME_TEAM":
        return match.home_team, match.away_team
    if match.winner == "AWAY_TEAM":
        return match.away_team, match.home_team
    return None, None


def _round_predictions(predictions: Optional[dict], label: str) -> list[dict]:
    return [p for p in (predictions or {}).values() if str(p.get("round_label", "")) == label]


def predicted_round_winner(predictions: Optional[dict], label: str, team_en: str) -> bool:
    """True if the user picked team_en to win one of their matches in this round."""
    for p in _round_predictions(predictions, label):
        pick = p.get("prediction")
        if is_knockout_prediction(pick) and names_match(pick, team_en):
            return True
    return False


def predicted_round_participant(predictions: Optional[dict], label: str, team_en: str) -> bool:
    """True if team_en appears (home or away) in the user's predicted bracket for this round."""
    for p in _round_predictions(predictions, label):
        if names_match(p.get("home_team", ""), team_en) or names_match(p.get("away_team", ""), team_en):
            return True
    return False


def predicted_final_runner_up(predictions: Optional[dict], team_en: str) -> bool:
    """True if the user's predicted Final loser (the finalist they did NOT pick) is team_en."""
    for p in _round_predictions(predictions, "Final"):
        pick = p.get("prediction")
        if not is_knockout_prediction(pick):
            continue
        home, away = p.get("home_team", ""), p.get("away_team", "")
        predicted_loser = away if names_match(pick, home) else home
        if names_match(predicted_loser, team_en):
            return True
    return False


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
    user_predictions: Optional[dict] = None,
    points_loser_advancement: int = 0,
) -> PointsBreakdown:
    """Split a prediction's points into the scoreline component and the advancing-team
    component (knockout only). The two are scored independently.

    Scoreline points need the user's predicted pairing to match the real fixture
    (prediction / predicted goals are for the matched fixture, or None).

    Advancement points follow the Excel template's "Equipo clasificado" rules and are
    matchup-independent: the advancing team earns the bonus if the user predicted it to
    reach the next round anywhere in their bracket (user_predictions), even when their
    predicted pairing for this fixture was different or missing.
      - R32/R16/QF/3rd place: +advancement if the winner is one of the user's picks for
        this round.
      - Semi-finals: +advancement if the winner is in the user's predicted Final,
        +loser_advancement if the loser is in the user's predicted 3rd-place match.
      - Final: +advancement (champion bonus) if the winner is the user's predicted
        champion; +runner_up if the loser is the user's predicted losing finalist.
        The two are independent (an exact predicted final earns both)."""
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

    # Advancing-team component (knockout only, matchup-independent)
    advancement_points = 0
    advanced = False
    runner_up = False
    label = STAGE_ROUND_LABEL.get(match.stage or "")
    if label and user_predictions:
        winner_en, loser_en = winner_loser_teams(match)
        if winner_en:
            if match.stage == "FINAL":
                if predicted_round_winner(user_predictions, "Final", winner_en):
                    advanced = True
                    advancement_points += points_advancement
                if points_runner_up > 0 and predicted_final_runner_up(user_predictions, loser_en):
                    runner_up = True
                    advancement_points += points_runner_up
            elif match.stage == "SEMI_FINALS":
                if predicted_round_participant(user_predictions, "Final", winner_en):
                    advanced = True
                    advancement_points += points_advancement
                if points_loser_advancement > 0 and predicted_round_participant(user_predictions, "3/4", loser_en):
                    advanced = True
                    advancement_points += points_loser_advancement
            else:
                if predicted_round_winner(user_predictions, label, winner_en):
                    advanced = True
                    advancement_points += points_advancement

    return PointsBreakdown(score_points, advancement_points, score_cv, advanced, runner_up)


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
        return f"🎫 {prediction}  <b>+{bd.advancement_points} pts</b>"
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
        if breakdown is not None and breakdown.advancement_points > 0:
            winner_en, _ = winner_loser_teams(match)
            return (
                f"⚽ <b>Match finished</b>\n"
                f"<b>{result_line}</b>\n\n"
                f"Match not predicted  +0 pts\n"
                f"Advancement prediction: 🎫 <b>{winner_en}</b>  <b>+{breakdown.advancement_points} pts</b>\n"
                f"<b>+{breakdown.advancement_points} pts</b>  (total: {total_points} pts)"
            )
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
    prediction: Optional[str],
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

    if prediction is None:
        winner_en, _ = winner_loser_teams(match)
        if bd.advancement_points > 0:
            detail = (f"Match not predicted  +0 pts\n"
                      f"Advancement prediction: 🎫 <b>{winner_en}</b>  <b>+{bd.advancement_points} pts</b>")
        else:
            detail = "Match not predicted."
        return (
            f"⚠️ <b>Score correction</b>\n"
            f"<b>{result_line}</b> (was {old_home}–{old_away})\n\n"
            f"{detail}\n"
            f"<b>+{new_points} pts</b>  (pts: {pts_change}, total: {total_points} pts)"
        )

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


async def send_message_ex(bot_token: str, chat_id: str, text: str) -> tuple[bool, bool]:
    """Send a message and return (sent, permanent_failure).

    permanent_failure is True on a 4xx response (chat not found, bot blocked, …):
    retrying will never succeed, so scoring records should be persisted anyway.
    Network errors and 5xx responses are transient (False, False)."""
    url = _TELEGRAM_URL.format(token=bot_token)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            if r.status_code == 200:
                return True, False
            if 400 <= r.status_code < 500:
                logger.warning(f"Telegram permanently rejected message for chat_id={chat_id}: "
                               f"{r.status_code} {r.text[:200]}")
                return False, True
            return False, False
        except httpx.RequestError as e:
            logger.error(f"Telegram request failed for chat_id={chat_id}: {e}")
            return False, False


async def send_message(bot_token: str, chat_id: str, text: str) -> bool:
    sent, _ = await send_message_ex(bot_token, chat_id, text)
    return sent
