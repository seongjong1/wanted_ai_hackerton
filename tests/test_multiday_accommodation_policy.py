"""Multi-day Accommodation Policy — date-driven lodging, no overnight FREE_TIME."""
from __future__ import annotations

from datetime import datetime, timedelta, time

import pytest

from config import load_settings
from models.access import AccessPoint
from models.replan import ReplanContext, ReplanEvent, ReplanEventType, item_key
from models.schedule import ReturnStatus
from models.transport import KST
from models.trip_request import Preference, TripRequest
from services.multiday_schedule_service import (
    is_multiday,
    overnight_gap_in_days,
    trip_night_count,
    validate_multiday,
    validate_multiday_reason,
)
from services.replan_service import replan_trip_schedule
from services.replan_validate import (
    accommodation_boundaries_ok,
    no_overnight_free_gap,
    _flat_items,
)
from services.schedule_day_view import build_schedule_views
from test_multiday_schedule import lodging, multiday_service, multiday_trip, pool
from test_places import trip, selected, anchor
from test_schedule_day_view import multiday_schedule


def test_case1_same_day_no_accommodation_required(trip):
    assert trip.start_date == trip.end_date
    assert not is_multiday(trip)
    assert trip_night_count(trip) == 0
    assert trip.has_accommodation is False


def test_case2_one_night_auto_multiday(trip):
    request = TripRequest(
        departure=trip.departure,
        destination=trip.destination,
        start_date=trip.start_date,
        end_date=trip.start_date + timedelta(days=1),
        departure_time=trip.departure_time,
        end_time=time(20),
        has_accommodation=False,
        allergies=trip.allergies,
        has_pet=trip.has_pet,
        has_child=trip.has_child,
        activity_radius=trip.activity_radius,
        preferences=(Preference.FOOD, Preference.SIGHTSEEING),
    )
    assert request.has_accommodation is True
    assert is_multiday(request)
    assert trip_night_count(request) == 1


def test_case3_two_nights(trip):
    request = multiday_trip(trip)  # +2 days
    assert trip_night_count(request) == 2
    assert is_multiday(request)


def test_case4_provisional_undecided(trip, selected, anchor):
    request = trip.model_copy(update={
        "end_date": trip.start_date + timedelta(days=1),
        "has_accommodation": False,
        "end_time": time(20),
        "preferences": (Preference.FOOD, Preference.SIGHTSEEING),
    })
    service, *_rest, hub = multiday_service(request, selected, anchor)
    result = service.generate(request, selected, pool(), "0", accommodation_undecided=True)
    assert result.schedule is not None
    assert result.schedule.accommodation_status == "PROVISIONAL"
    assert result.schedule.accommodation.id == hub.id
    assert len(result.schedule.days) == 2
    assert len(result.schedule.accommodation_nights) == 1


def test_case5_confirmed_accommodation(trip, selected, anchor):
    request = trip.model_copy(update={
        "end_date": trip.start_date + timedelta(days=1),
        "has_accommodation": True,
        "end_time": time(20),
        "preferences": (Preference.FOOD, Preference.SIGHTSEEING),
    })
    service, *_ = multiday_service(request, selected, anchor)
    stay = lodging()
    result = service.generate(request, selected, pool(), "0", "구미 테스트 숙소")
    assert result.schedule.accommodation_status == "CONFIRMED"
    assert result.schedule.accommodation.id == stay.id


def test_case6_7_8_no_overnight_free_time_and_boundaries(trip, selected, anchor):
    request = trip.model_copy(update={
        "end_date": trip.start_date + timedelta(days=1),
        "has_accommodation": True,
        "end_time": time(20),
        "preferences": (Preference.FOOD, Preference.SIGHTSEEING),
    })
    service, *_rest, hub = multiday_service(request, selected, anchor)
    result = service.generate(request, selected, pool(), "0", accommodation_undecided=True)
    schedule = result.schedule
    assert schedule is not None
    assert not overnight_gap_in_days(schedule)
    assert no_overnight_free_gap(schedule)
    assert schedule.days[0].end_location.id == hub.id
    assert schedule.days[1].start_location.id == hub.id
    views = build_schedule_views(schedule, selected=selected)
    for view in views:
        for event in view.timeline:
            if event.kind != "FREE_TIME" or event.end is None:
                continue
            assert event.start.date() == event.end.date()
            gap_min = (event.end - event.start).total_seconds() / 60
            assert gap_min < 600


def test_case9_middle_day_lodge_bookends(trip, selected, anchor):
    request = multiday_trip(trip)
    service, _transport, _home, stay, _back, _hub = multiday_service(request, selected, anchor)
    result = service.generate(request, selected, pool(), "0", "구미 테스트 숙소")
    schedule = result.schedule
    assert len(schedule.days) == 3
    middle = schedule.days[1]
    assert middle.role == "MIDDLE"
    assert middle.start_location.id == stay.id
    assert middle.end_location.id == stay.id


def test_case10_final_day_to_return(trip, selected, anchor):
    request = trip.model_copy(update={
        "end_date": trip.start_date + timedelta(days=1),
        "has_accommodation": True,
        "end_time": time(20),
        "preferences": (Preference.FOOD, Preference.SIGHTSEEING),
    })
    service, *_ = multiday_service(request, selected, anchor)
    result = service.generate(request, selected, pool(), "0", accommodation_undecided=True)
    schedule = result.schedule
    assert schedule.days[-1].role == "FINAL"
    assert schedule.return_status == ReturnStatus.RETURN_AVAILABLE
    assert schedule.return_journey is not None


def test_case11_nights_length_matches(trip, selected, anchor):
    request = multiday_trip(trip)
    service, *_ = multiday_service(request, selected, anchor)
    result = service.generate(request, selected, pool(), "0", accommodation_undecided=True)
    assert len(result.schedule.accommodation_nights) == trip_night_count(request) == 2
    assert validate_multiday(result.schedule, request, result.schedule.accommodation)


def test_case12_replan_keeps_accommodation_boundary(selected, trip):
    schedule = multiday_schedule()
    current = datetime(2026, 9, 19, 9, 20, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP,
        occurred_at=current,
        place_id="p2",
        source="USER_REPORTED",
    )
    result = replan_trip_schedule(
        ReplanContext(
            trip=trip.model_copy(update={
                "start_date": schedule.trip_start_datetime.date(),
                "end_date": schedule.trip_end_datetime.date(),
                "has_accommodation": True,
            }),
            original_schedule=schedule,
            selected_transport=selected,
            current_datetime=current,
            event=event,
            completed_item_ids=frozenset(
                item_key(i) for i in _flat_items(schedule) if i.end_datetime <= current
            ),
        ),
        load_settings({}, {}),
    )
    proposed = result.proposed_schedule
    assert proposed is not None
    assert accommodation_boundaries_ok(proposed)
    assert proposed.accommodation is not None
    assert len(proposed.accommodation_nights) == len(proposed.days) - 1
    assert proposed.days[0].end_location.id == proposed.accommodation.id
    assert proposed.days[1].start_location.id == proposed.accommodation.id


def test_case13_same_day_regression_path(trip):
    """Same-day trips stay non-multiday and do not force lodging UI path."""
    assert not is_multiday(trip)
    assert trip.has_accommodation is False


def test_case14_validator_rejects_missing_boundary(trip):
    schedule = multiday_schedule()
    broken = schedule.model_copy(update={"accommodation": None, "accommodation_nights": ()})
    assert not accommodation_boundaries_ok(broken)
    reason = validate_multiday_reason(broken, trip.model_copy(update={
        "start_date": schedule.trip_start_datetime.date(),
        "end_date": schedule.trip_end_datetime.date(),
        "has_accommodation": True,
    }), lodging())
    assert reason in {"accommodation_mismatch", "night_count", "accommodation_boundary_missing"}
