from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin


BASE_URL = "https://8bo.com/football/"


@dataclass(frozen=True)
class Route:
    name: str
    path: str
    field_family: str

    def url(self) -> str:
        return urljoin(BASE_URL, self.path)


def schedule_route(day: date) -> Route:
    return Route(
        name="schedule",
        path=f"schedule/{day:%Y%m%d}.html",
        field_family="schedule_and_event_identity",
    )


def event_routes(event_id: str, supplier_id: str = "1") -> tuple[Route, ...]:
    """Return the fixed, auditable route sequence for one 8BO event."""
    event_id = str(event_id)
    supplier_id = str(supplier_id)
    return (
        Route("event_summary", f"info-list/{event_id}-1-1/", "event_identity"),
        Route("three_way", f"info-321/{event_id}/", "three_way_and_market_mean"),
        Route("european", f"info-1x2/{event_id}/", "european_odds"),
        Route("asian_handicap", f"info-ah/{event_id}/", "asian_handicap"),
        Route("totals", f"info-ou/{event_id}/", "totals"),
        Route("correct_score_movement", f"info-list-bd/{event_id}-{supplier_id}/", "correct_score_movement"),
        Route("total_goals_movement", f"info-list-rqs/{event_id}-{supplier_id}/", "total_goals_movement"),
        Route("half_full_movement", f"info-list-bqc/{event_id}-{supplier_id}/", "half_full_movement"),
        Route("betfair", f"info-betfair/{event_id}/", "betfair_and_fund_flow"),
    )

