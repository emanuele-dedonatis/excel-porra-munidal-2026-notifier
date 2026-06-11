from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
_DATE_TOLERANCE = timedelta(minutes=5)


async def fetch_score(home_team: str, away_team: str, kickoff_utc: datetime) -> Optional[tuple[int, int]]:
    """
    Return (home_score, away_score) from ESPN for the given match, or None if not found/finished.
    Matches by kickoff time (within 5 min tolerance) since team name formats may differ.
    """
    date_str = kickoff_utc.strftime("%Y%m%d")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(_SCOREBOARD_URL, params={"dates": date_str})
        r.raise_for_status()
        data = r.json()

    for event in data.get("events", []):
        try:
            event_date = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue

        if abs(event_date - kickoff_utc) > _DATE_TOLERANCE:
            continue

        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)

        if not home or not away:
            continue

        status = event.get("status", {}).get("type", {}).get("name", "")
        if status not in ("STATUS_FULL_TIME", "STATUS_FINAL"):
            continue

        try:
            return int(home["score"]), int(away["score"])
        except (KeyError, ValueError, TypeError):
            continue

    return None
