"""Opt-in API integration; uses runtime configuration without logging secrets."""
import os
from datetime import datetime, time

import pytest

from config import load_settings
from models.transport import KST
from models.trip_request import TripRequest
from services.transport_service import search_transport
from services.place_service import search_places_for_trip
from services.schedule_service import generate_trip_schedule

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_SCHEDULE_TESTS") != "1", reason="Opt-in schedule integration")


def test_live_local_origin_food_schedule():
    settings = load_settings()
    if not settings.kakao_rest_api_key or not settings.data_go_kr_api_key:
        pytest.skip("API credentials required")
    day = datetime.now(KST).date()
    trip = TripRequest(departure="구로디지털단지역", destination="구미", start_date=day, end_date=day,
                       departure_time=time(9), end_time=time(20), has_accommodation=False,
                       has_pet=False, has_child=False, activity_radius="500m 이상", preferences=["맛집"])
    transport = search_transport(trip, settings)
    assert transport.candidates
    selected = transport.candidates[0]
    places = search_places_for_trip(trip, selected, settings)
    assert places.candidates
    result = generate_trip_schedule(trip, selected, places.candidates, settings)
    assert result.schedule and result.schedule.validation_status == "VALIDATED"
    assert result.schedule.trip_start_datetime == selected.arrival_time
    assert result.schedule.items[-1].end_datetime <= datetime.combine(day, trip.end_time, KST)
    assert len([i for i in result.schedule.items if i.item_type == "MEAL"]) <= 2
