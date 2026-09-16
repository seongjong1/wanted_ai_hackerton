from datetime import datetime

import pytest
from pydantic import ValidationError

from models.access import AccessLeg, BoardingAssessment
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
