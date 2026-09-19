from datetime import datetime

import pytest
from pydantic import ValidationError

from models.access import (
    AccessLeg, AccessPoint, BoardingAssessment,
    access_hub_label, is_estimated_access, is_trivial_same_place_access,
)
from models.transport import KST


def leg(minutes=30):
    return AccessLeg(origin="서울역", destination="서울경부", transport_modes=("SUBWAY", "WALKING"),
                     duration_minutes=minutes, departure_time=datetime(2026, 10, 1, 9, tzinfo=KST),
                     provider="test fixture")


@pytest.mark.parametrize("minute,feasible,waiting", [(35, False, -10), (45, True, 0), (50, True, 5)])
def test_boarding_example_and_exact_boundary(minute, feasible, waiting):
    result = BoardingAssessment(leg(), 15, datetime(2026, 10, 1, 9, minute, tzinfo=KST),
                                datetime(2026, 10, 1, 13, tzinfo=KST))
    assert result.ready_time == datetime(2026, 10, 1, 9, 45, tzinfo=KST)
    assert result.is_feasible is feasible
    assert result.waiting_minutes == waiting
    assert result.total_duration_minutes == 240


def test_midnight_and_no_double_counting():
    access = leg().model_copy(update={"departure_time": datetime(2026, 10, 1, 23, 30, tzinfo=KST)})
    result = BoardingAssessment(access, 15, datetime(2026, 10, 2, 0, 30, tzinfo=KST),
                                datetime(2026, 10, 2, 2, tzinfo=KST))
    assert result.is_feasible
    assert result.waiting_minutes == 15
    assert result.total_duration_minutes == 150  # access 30 + buffer 15 + wait 15 + train 90


@pytest.mark.parametrize("minutes", [-1, float("nan"), float("inf")])
def test_invalid_duration(minutes):
    with pytest.raises(ValidationError):
        leg(minutes)


def test_same_hub_still_requires_buffer():
    result = BoardingAssessment(leg(0), 15, datetime(2026, 10, 1, 9, 10, tzinfo=KST),
                                datetime(2026, 10, 1, 12, tzinfo=KST))
    assert not result.is_feasible


def test_user_28_minute_example():
    result = BoardingAssessment(leg(28), 15, datetime(2026, 10, 1, 9, 35, tzinfo=KST),
                                datetime(2026, 10, 1, 13, tzinfo=KST))
    assert result.ready_time.minute == 43
    assert not result.is_feasible


def test_confirmed_access_leg_estimated_defaults_false():
    access = leg()
    assert "estimated" in AccessLeg.model_fields
    assert access.estimated is False
    assert is_estimated_access(access) is False


def test_is_estimated_access_keeps_legacy_provider_and_mode_markers():
    from types import SimpleNamespace
    confirmed = SimpleNamespace(provider="Kakao publictraffic", transport_modes=("SUBWAY",))
    assert not hasattr(confirmed, "estimated")
    assert is_estimated_access(confirmed) is False
    by_provider = SimpleNamespace(provider="ESTIMATED", transport_modes=("WALKING",))
    assert is_estimated_access(by_provider) is True
    by_mode = SimpleNamespace(provider="Kakao publictraffic", transport_modes=("ESTIMATED",))
    assert is_estimated_access(by_mode) is True
    by_flag = SimpleNamespace(estimated=True, provider="Kakao publictraffic", transport_modes=("SUBWAY",))
    assert is_estimated_access(by_flag) is True
    explicit = AccessLeg(origin="서울역", destination="서울경부", transport_modes=("WALKING",),
                         duration_minutes=20, departure_time=datetime(2026, 10, 1, 9, tzinfo=KST),
                         provider="ESTIMATED", estimated=False)
    assert explicit.estimated is False
    assert is_estimated_access(explicit) is True


def test_trivial_same_place_access_zero_minutes():
    station = AccessPoint(id="station", name="서울역", x=126.97, y=37.55)
    same = AccessLeg(
        origin="서울역", destination="서울역", transport_modes=("SAME_PLACE",),
        duration_minutes=0, distance_meters=0,
        departure_time=datetime(2026, 10, 1, 9, tzinfo=KST),
        provider="Kakao Local · same place ID",
        origin_point=station, destination_point=station)
    assert is_trivial_same_place_access(same) is True
    assert access_hub_label(same) == "서울역"
    real = leg(49)
    assert is_trivial_same_place_access(real) is False
    mismatched_zero = AccessLeg(
        origin="서울", destination="서울역", transport_modes=("SAME_PLACE",),
        duration_minutes=0, departure_time=datetime(2026, 10, 1, 9, tzinfo=KST),
        provider="test", origin_point=station, destination_point=station)
    assert is_trivial_same_place_access(mismatched_zero) is True
    assert access_hub_label(mismatched_zero) == "서울역"
