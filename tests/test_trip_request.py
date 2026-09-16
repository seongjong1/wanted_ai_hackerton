from datetime import date, time

import pytest
from pydantic import ValidationError

from models.trip_request import RADIUS_OPTIONS, TripRequest


@pytest.fixture
def trip_data():
    return dict(departure=" 서울역 ", destination="부산", start_date=date(2026, 10, 1),
                end_date=date(2026, 10, 1), departure_time=time(9), end_time=time(20),
                has_accommodation=False, allergies=["땅콩", " 땅콩 ", ""], has_pet=False,
                has_child=True, activity_radius="500m", preferences=["맛집"])


def test_valid_trip_and_normalization(trip_data):
    trip = TripRequest(**trip_data)
    assert trip.departure == "서울역"
    assert trip.allergies == ("땅콩",)
    assert trip.model_dump(mode="json")["start_date"] == "2026-10-01"


@pytest.mark.parametrize("changes", [
    {"departure": "   "}, {"destination": ""}, {"destination": "서 울 역"},
    {"end_date": date(2026, 9, 30)}, {"end_time": time(9)}, {"end_time": time(8)},
    {"preferences": []}, {"preferences": ["unknown"]}, {"activity_radius": "600m"},
    {"activity_radius": 500}, {"allergies": ["a" * 101]},
])
def test_invalid_trip(trip_data, changes):
    with pytest.raises(ValidationError):
        TripRequest(**{**trip_data, **changes})


@pytest.mark.parametrize("radius", RADIUS_OPTIONS)
def test_all_radii(trip_data, radius):
    assert TripRequest(**{**trip_data, "activity_radius": radius}).activity_radius == radius


def test_overnight_allows_earlier_clock_time(trip_data):
    assert TripRequest(**{**trip_data, "end_date": date(2026, 10, 2), "end_time": time(8)})
