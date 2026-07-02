from typing import Optional

from .team_names import names_match

_GROUP_ROUND_LABELS = {"J1", "J2", "J3"}


def _team_in_group(spanish_name: str, group_teams_en: list[str]) -> Optional[str]:
    """Return the English team name if spanish_name maps to one in group_teams_en, else None."""
    for en in group_teams_en:
        if names_match(spanish_name, en):
            return en
    return None


def simulate_predicted_standings(
    predictions_json: dict,
    group_teams_en: list[str],
) -> Optional[list[str]]:
    """
    Derive the user's predicted group standings from their stored match predictions.

    Returns a list of 4 English team names ordered 1→4 by predicted standing,
    or None if fewer than 6 group matches were found in the user's predictions.
    """
    table: dict[str, dict] = {
        team: {"pts": 0, "gd": 0, "gf": 0} for team in group_teams_en
    }
    matches_found = 0

    for pred in predictions_json.values():
        if str(pred.get("round_label", "")) not in _GROUP_ROUND_LABELS:
            continue

        home_es = pred.get("home_team", "")
        away_es = pred.get("away_team", "")

        home_en = _team_in_group(home_es, group_teams_en)
        away_en = _team_in_group(away_es, group_teams_en)

        if home_en is None or away_en is None:
            continue

        prediction = pred.get("prediction", "")
        pred_home_g = pred.get("predicted_home_goals")
        pred_away_g = pred.get("predicted_away_goals")

        if prediction == "home":
            table[home_en]["pts"] += 3
        elif prediction == "away":
            table[away_en]["pts"] += 3
        elif prediction == "draw":
            table[home_en]["pts"] += 1
            table[away_en]["pts"] += 1

        if pred_home_g is not None and pred_away_g is not None:
            diff = int(pred_home_g) - int(pred_away_g)
            table[home_en]["gd"] += diff
            table[away_en]["gd"] -= diff
            table[home_en]["gf"] += int(pred_home_g)
            table[away_en]["gf"] += int(pred_away_g)

        matches_found += 1

    if matches_found < 6:
        return None

    # Perfect ties (same pts/gd/gf) fall back to the incoming group_teams_en order —
    # the actual standings order — which is how the Excel template resolves them.
    return sorted(
        group_teams_en,
        key=lambda t: (-table[t]["pts"], -table[t]["gd"], -table[t]["gf"], group_teams_en.index(t)),
    )


def predicted_r32_bracket(predictions_json: dict) -> list[str]:
    """The 32 teams (Spanish names) in the user's predicted Round-of-32 bracket."""
    teams: list[str] = []
    for pred in (predictions_json or {}).values():
        if str(pred.get("round_label", "")) == "1/32":
            for key in ("home_team", "away_team"):
                name = pred.get(key, "")
                if name:
                    teams.append(name)
    return teams


def compute_r32_advancement_points(
    group_teams_en: list[str],
    bracket_teams_es: list[str],
    r32_teams: set[str],
) -> int:
    """1 pt per team of this group that qualified for the R32 AND appears in the user's
    predicted R32 bracket (matchup-independent, like the Excel's 'Equipo clasificado
    para dieciseisavos' rule)."""
    pts = 0
    for team_en in group_teams_en:
        if team_en not in r32_teams:
            continue
        if any(names_match(team_es, team_en) for team_es in bracket_teams_es):
            pts += 1
    return pts


def compute_ranking_points(
    predicted: list[str],
    actual: list[str],
    pts_per_pos: int,
) -> int:
    return sum(
        pts_per_pos
        for p, a in zip(predicted, actual)
        if p == a
    )


def build_group_ranking_message(
    group_name: str,
    predicted: list[str],
    actual: list[str],
    pts_earned: int,
    total_pts: int,
) -> str:
    ordinals = ["1st", "2nd", "3rd", "4th"]
    lines = [f"🏆 <b>{group_name} final standings</b>\n"]
    for i, (pred_team, actual_team) in enumerate(zip(predicted, actual)):
        icon = "✅" if pred_team == actual_team else "❌"
        lines.append(f"{ordinals[i]}:  {icon}  <b>{actual_team}</b>  (you predicted: {pred_team})")
    lines.append(f"\nGroup ranking: <b>+{pts_earned} pts</b>  (total: {total_pts} pts)")
    return "\n".join(lines)
