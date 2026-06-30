from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx

_BASE_URL = "https://api.football-data.org/v4"
_COMPETITION = "WC"


@dataclass
class APIMatch:
    id: int
    home_team: str
    away_team: str
    kickoff_utc: datetime
    status: str           # SCHEDULED | TIMED | IN_PLAY | PAUSED | FINISHED | …
    home_score: Optional[int]
    away_score: Optional[int]
    winner: Optional[str]   # HOME_TEAM | AWAY_TEAM | DRAW (reliable for extra-time/penalties)
    duration: Optional[str] # REGULAR | EXTRA_TIME | PENALTY_SHOOTOUT
    stage: str
    matchday: Optional[int]


async def get_matches(api_key: str, status: Optional[str] = None, stage: Optional[str] = None) -> list[APIMatch]:
    params: dict = {}
    if status:
        params["status"] = status
    if stage:
        params["stage"] = stage

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{_BASE_URL}/competitions/{_COMPETITION}/matches",
            params=params,
            headers={"X-Auth-Token": api_key},
        )
        response.raise_for_status()
        data = response.json()

    matches = []
    for m in data.get("matches", []):
        try:
            kickoff_utc = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue

        score = m.get("score", {})
        full_time = score.get("fullTime", {})
        duration = score.get("duration")
        winner = score.get("winner")
        home_score = full_time.get("home")
        away_score = full_time.get("away")

        if duration == "PENALTY_SHOOTOUT":
            # For a shootout, fullTime carries the regulation+penalties combined number.
            # Points are scored on the result before penalties (end of extra time), so use
            # regularTime + extraTime instead.
            regular = score.get("regularTime", {}) or {}
            extra = score.get("extraTime", {}) or {}
            reg_home, reg_away = regular.get("home"), regular.get("away")
            if reg_home is not None and reg_away is not None:
                home_score = reg_home + (extra.get("home") or 0)
                away_score = reg_away + (extra.get("away") or 0)
            # The API sometimes leaves winner null while resolving; the combined fullTime
            # still encodes who won the shootout, so derive it from there.
            if winner is None and full_time.get("home") is not None and full_time.get("away") is not None:
                if full_time["home"] > full_time["away"]:
                    winner = "HOME_TEAM"
                elif full_time["home"] < full_time["away"]:
                    winner = "AWAY_TEAM"

        matches.append(APIMatch(
            id=m["id"],
            home_team=m["homeTeam"]["name"],
            away_team=m["awayTeam"]["name"],
            kickoff_utc=kickoff_utc,
            status=m["status"],
            home_score=home_score,
            away_score=away_score,
            winner=winner,
            duration=duration,
            stage=m.get("stage", ""),
            matchday=m.get("matchday"),
        ))

    return matches


async def get_finished_matches(api_key: str) -> list[APIMatch]:
    return await get_matches(api_key, status="FINISHED")


async def get_standings(api_key: str) -> list[dict]:
    """Return group standings as a list of dicts with group name, ordered team names, and games played."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{_BASE_URL}/competitions/{_COMPETITION}/standings",
            headers={"X-Auth-Token": api_key},
        )
        response.raise_for_status()
        data = response.json()

    result = []
    for entry in data.get("standings", []):
        group = entry.get("group", "")
        table = entry.get("table", [])
        if not group or not table:
            continue
        played = min(row.get("playedGames", 0) for row in table)
        teams = [row["team"]["name"] for row in table if row.get("team", {}).get("name")]
        result.append({"group": group, "teams": teams, "played": played})
    return result
