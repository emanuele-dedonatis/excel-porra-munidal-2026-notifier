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


async def get_matches(api_key: str, status: Optional[str] = None) -> list[APIMatch]:
    params: dict = {}
    if status:
        params["status"] = status

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

        matches.append(APIMatch(
            id=m["id"],
            home_team=m["homeTeam"]["name"],
            away_team=m["awayTeam"]["name"],
            kickoff_utc=kickoff_utc,
            status=m["status"],
            home_score=full_time.get("home"),
            away_score=full_time.get("away"),
            winner=score.get("winner"),
            duration=score.get("duration"),
            stage=m.get("stage", ""),
            matchday=m.get("matchday"),
        ))

    return matches


async def get_finished_matches(api_key: str) -> list[APIMatch]:
    return await get_matches(api_key, status="FINISHED")
