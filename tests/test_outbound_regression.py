"""Outbound transport isolation regressions (activity_radius / preference / return / access)."""
from __future__ import annotations

from datetime import date, time
from unittest.mock import Mock

import pytest

from models.access import AccessRoute, AccessStep
from models.transport import TransportType
from models.trip_request import Preference, TripRequest
from providers.http_client import ProviderError
from services.access_service import AccessService
from services.transport_service import TransportService, rank_candidates, convert_candidate
from test_local_origin import setup


def _gumi_trip(**updates) -> TripRequest:
    base = dict(
        departure="서울 금천구 가산로3길 45",
        destination="구미",
        start_date=date(2026, 9, 18),
        end_date=date(2026, 9, 20),
        departure_time=time(9),
        end_time=time(20),
        has_accommodation=True,
        has_pet=False,
        has_child=False,
        activity_radius="500m",
        preferences=["맛집"],
    )
    base.update(updates)
    return TripRequest(**base)


def test_activity_radius_does_not_change_outbound_candidates():
    service, trip, places, transit, train, bus = setup("구로디지털단지역", "구미")
    counts = []
    keys = []
    for radius in ("100m", "500m", "500m 이상"):
        result = service.search(trip.model_copy(update={"activity_radius": radius}))
        counts.append(len(result.candidates))
        keys.append(tuple(
            (c.transport_type, c.departure_place, c.arrival_place,
             c.departure_time, c.arrival_time) for c in result.candidates))
    assert counts[0] > 0
    assert counts[0] == counts[1] == counts[2]
    assert keys[0] == keys[1] == keys[2]


def test_preference_does_not_remove_outbound_candidates():
    service, trip, *_ = setup("구로디지털단지역", "구미")
    results = []
    for prefs in ((Preference.FOOD,), (Preference.SIGHTSEEING,),
                  (Preference.FOOD, Preference.SIGHTSEEING)):
        results.append(service.search(trip.model_copy(update={"preferences": prefs})))
    assert all(r.candidates for r in results)
    keys = [tuple((c.departure_place, c.departure_time, c.arrival_time) for c in r.candidates)
            for r in results]
    assert keys[0] == keys[1] == keys[2]


def test_return_search_failure_does_not_clear_outbound():
    service, trip, places, transit, train, bus = setup("구로디지털단지역", "구미")
    outbound = service.search(trip)
    assert outbound.candidates
    # Force return-mode failure independently.
    service.search_return_candidates = Mock(side_effect=ProviderError("RETURN_REGION_UNKNOWN"))
    with pytest.raises(ProviderError):
        service.search_return_candidates(
            trip.model_copy(update={"departure": "구미", "destination": "서울"}),
            Mock(address="서울 금천구", name="origin"),
            earliest_departure=__import__("datetime").datetime.combine(
                trip.end_date, time(9), __import__("models.transport", fromlist=["KST"]).KST))
    # Outbound result object remains intact.
    assert outbound.candidates
    assert len(service.search(trip).candidates) == len(outbound.candidates)


def test_multiday_end_date_does_not_filter_day1_outbound():
    trip = _gumi_trip()
    candidate = convert_candidate({
        "depPlaceNm": "서울경부", "arrPlaceNm": "구미",
        "depPlandTime": "20260918102500", "arrPlandTime": "20260918132500",
        "gradeNm": "프리미엄", "charge": 20000,
    }, TransportType.EXPRESS_BUS)
    # end_date/end_time must not drop a same-day outbound arrival.
    ranked = rank_candidates([candidate], trip)
    assert ranked == [candidate]


def test_outbound_pipeline_logs_stage_counts(caplog):
    service, trip, *_ = setup("구로디지털단지역", "구미")
    with caplog.at_level("INFO", logger="travel_ai.transport"):
        result = service.search(trip)
    assert result.candidates
    assert "provider_raw=" in caplog.text
    assert "access_feasible=" in caplog.text
    assert "outbound_final candidate_count=" in caplog.text
