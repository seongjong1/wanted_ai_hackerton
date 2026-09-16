"""Explicit opt-in network test; only reads environment secrets, never prints them."""
import os
from datetime import date,time

import pytest

from config import load_settings
from models.trip_request import TripRequest
from models.transport import TransportType
from services.place_service import search_places_for_trip
from services.transport_service import convert_candidate

pytestmark=pytest.mark.skipif(os.environ.get("RUN_LIVE_PLACE_TESTS")!="1",reason="Opt-in API integration test")


def test_live_place_search():
    settings=load_settings()
    if not settings.kakao_rest_api_key: pytest.skip("Kakao key is required")
    # Transport fixture identifies the anchor; no claim of a real train or timetable.
    selected=convert_candidate({"depplacename":"서울","arrplacename":"구미","depplandtime":"20261001090000",
                                "arrplandtime":"20261001120000"},TransportType.TRAIN)
    trip=TripRequest(departure="서울역",destination="구미",start_date=date(2026,10,1),end_date=date(2026,10,1),
                     departure_time=time(9),end_time=time(20),has_accommodation=False,has_pet=False,has_child=False,
                     activity_radius="500m",preferences=["맛집"])
    result=search_places_for_trip(trip,selected,settings,nearby=True)
    assert result.anchor and result.anchor.name=="구미역"
    assert result.candidates
    assert all(c.distance_meters<=500 and c.place_id for c in result.candidates)
