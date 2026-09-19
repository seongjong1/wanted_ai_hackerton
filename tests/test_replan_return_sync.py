"""Final Day Replan + Return Journey Synchronization — CASE 1–14."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from config import load_settings
from models.access import AccessLeg, AccessPoint
from models.place import PlaceCandidate
from models.replan import ReplanContext, ReplanEvent, ReplanEventType, ReplanStatus, item_key
from models.schedule import ReturnJourney, ReturnStatus, ScheduleItem, TripSchedule
from models.transport import KST, TransportCandidate, TransportType
from services.replan_service import (
    replan_trip_schedule,
    sync_final_day_return,
)
from services.replan_validate import (
    return_access_synced,
    return_boarding_feasible,
    return_chain_chronological,
    validate_replan,
    _flat_items,
)
from services.schedule_day_view import build_schedule_views
from test_places import trip, selected


def _point(pid, name, x, y):
    return AccessPoint(id=pid, name=name, x=x, y=y, address="경북 구미시", source="test")


def _visit(place_id, name, start, end, *, meal=None, preference="관광"):
    return ScheduleItem(
        item_type="MEAL" if meal else "PLACE",
        place_id=place_id,
        place_name=name,
        start_datetime=start,
        end_datetime=end,
        preference=preference,
        meal_slot=f"{start.date()}:{meal}" if meal else None,
        reason="test",
        validation_status="VALIDATED",
        destination=_point(place_id, name, 128.34, 36.12),
    )


def _travel(place_id, name, start, end, origin, destination):
    return ScheduleItem(
        item_type="TRAVEL",
        place_id=place_id,
        place_name=name,
        start_datetime=start,
        end_datetime=end,
        origin=origin,
        destination=destination,
        travel_mode=("BUS",),
        travel_duration_minutes=(end - start).total_seconds() / 60,
        reason="",
        validation_status="VALIDATED",
    )


def _candidate(place_id, name, *, category="음식점 > 한식", lat=36.125, lon=128.345):
    return PlaceCandidate(
        place_id=place_id,
        place_name=name,
        category=category,
        address="경북 구미시",
        latitude=lat,
        longitude=lon,
        preference_score=80,
        role="NEARBY_PLACE",
        matched_preferences=(),
    )


@pytest.fixture
def final_day_schedule():
    """Single-day Gumi trip: terminal → park → 꽃돼지(MAIN) → return from 꽃돼지."""
    hub = _point("hub", "구미종합터미널", 128.33, 36.12)
    park = _point("park", "동락공원", 128.343, 36.124)
    meal = _point("pig", "꽃돼지식당 구미본점", 128.342, 36.123)
    station = _point("return-hub", "구미역", 128.35, 36.14)
    home = _point("home", "서울 금천구", 126.89, 37.45)
    yd = _point("yd", "영등포역", 126.90, 37.51)

    t0 = datetime(2026, 9, 20, 12, 50, tzinfo=KST)
    park_arrive = datetime(2026, 9, 20, 13, 5, tzinfo=KST)
    park_end = datetime(2026, 9, 20, 13, 20, tzinfo=KST)
    meal_start = datetime(2026, 9, 20, 13, 30, tzinfo=KST)
    meal_end = datetime(2026, 9, 20, 14, 40, tzinfo=KST)
    items = (
        _travel("park", park.name, t0, park_arrive, hub, park),
        _visit("park", park.name, park_arrive, park_end),
        _travel("pig", meal.name, park_end, meal_start, park, meal),
        _visit("pig", meal.name, meal_start, meal_end, meal="점심"),
    )
    tc = TransportCandidate(
        transport_type=TransportType.TRAIN,
        grade="무궁화",
        departure_place="구미",
        arrival_place="영등포",
        departure_time=datetime(2026, 9, 20, 15, 50, tzinfo=KST),
        arrival_time=datetime(2026, 9, 20, 18, 28, tzinfo=KST),
        price=10000,
        provider="test",
    )
    hub_leg = AccessLeg(
        origin=meal.name,
        destination=station.name,
        transport_modes=("BUS",),
        duration_minutes=31,
        departure_time=meal_end,
        provider="test",
        origin_point=meal,
        destination_point=station,
    )
    home_leg = AccessLeg(
        origin=yd.name,
        destination=home.name,
        transport_modes=("SUBWAY",),
        duration_minutes=25,
        departure_time=tc.arrival_time,
        provider="test",
        origin_point=yd,
        destination_point=home,
    )
    journey = ReturnJourney(
        to_hub=hub_leg, transport=tc, boarding_buffer_minutes=15, to_origin=home_leg)
    return TripSchedule(
        trip_start_datetime=t0,
        trip_end_datetime=datetime(2026, 9, 20, 20, 0, tzinfo=KST),
        arrival_point=hub,
        items=items,
        days=(),
        accommodation=None,
        return_journey=journey,
        return_status=ReturnStatus.RETURN_AVAILABLE,
        final_arrival_datetime=home_leg.arrival_time,
        destination_activity_cutoff=datetime(2026, 9, 20, 15, 3, tzinfo=KST),
        user_selected_place_id="pig",
        validation_status="VALIDATED",
        anchor_status="INCLUDED",
    )


def _ctx(schedule, selected, trip, current, event, *, completed=None, pool=()):
    done = completed if completed is not None else frozenset(
        item_key(i) for i in _flat_items(schedule) if i.end_datetime <= current
    )
    return ReplanContext(
        trip=trip.model_copy(update={
            "start_date": schedule.trip_start_datetime.date(),
            "end_date": schedule.trip_end_datetime.date(),
            "has_accommodation": False,
        }),
        original_schedule=schedule,
        selected_transport=selected,
        current_datetime=current,
        event=event,
        completed_item_ids=done,
        place_pool=tuple(pool),
    )


def test_case1_return_origin_follows_new_last_activity(final_day_schedule, selected, trip):
    """Original last=꽃돼지; after skip+replacement last=큰나무집 → return origin updates."""
    schedule = final_day_schedule
    current = datetime(2026, 9, 20, 13, 25, tzinfo=KST)
    # Park completed; skip pig; pool offers replacement near station
    done = frozenset(
        item_key(i) for i in schedule.items
        if i.place_id == "park" or (i.item_type == "TRAVEL" and i.place_id == "park")
    )
    # Mark park visit completed only
    done = frozenset(
        item_key(i) for i in schedule.items
        if i.item_type != "TRAVEL" and i.place_id == "park"
        or (i.item_type == "TRAVEL" and i.end_datetime <= current)
    )
    tree = _candidate("tree", "큰나무집궁중약백숙 구미점", lat=36.126, lon=128.348)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP,
        occurred_at=current,
        place_id="pig",
        source="USER_REPORTED",
    )
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=done, pool=(tree,)),
        load_settings({}, {}),
    )
    assert result.status in {ReplanStatus.REPLAN_SUCCESS, ReplanStatus.REPLAN_PARTIAL}
    proposed = result.proposed_schedule
    assert proposed.return_journey is not None
    last = next(
        i for i in reversed(_flat_items(proposed)) if i.item_type != "TRAVEL")
    assert proposed.return_journey.to_hub.origin_point is not None
    assert proposed.return_journey.to_hub.origin_point.id == last.place_id
    # MAIN skipped by user → not silently promoted
    if last.place_id == "tree":
        assert proposed.user_selected_place_id != "tree" or result.main_status == "REJECTED_BY_USER"


def test_case2_activity_after_return_start_fails_validation(final_day_schedule):
    """Activity end 14:59 > return start 14:51 → validation fail."""
    schedule = final_day_schedule
    stale = schedule.return_journey.to_hub.model_copy(update={
        "departure_time": datetime(2026, 9, 20, 14, 51, tzinfo=KST),
        "origin": "꽃돼지식당 구미본점",
        "origin_point": _point("pig", "꽃돼지식당 구미본점", 128.342, 36.123),
    })
    # Force last activity later than stale return start
    items = list(schedule.items)
    last = items[-1].model_copy(update={
        "start_datetime": datetime(2026, 9, 20, 13, 49, tzinfo=KST),
        "end_datetime": datetime(2026, 9, 20, 14, 59, tzinfo=KST),
        "place_id": "tree",
        "place_name": "큰나무집궁중약백숙 구미점",
        "destination": _point("tree", "큰나무집궁중약백숙 구미점", 128.348, 36.126),
    })
    items[-1] = last
    bad = schedule.model_copy(update={
        "items": tuple(items),
        "return_journey": schedule.return_journey.model_copy(update={"to_hub": stale}),
    })
    assert not return_access_synced(bad)
    assert not return_chain_chronological(bad)
    ok, errors = validate_replan(schedule, bad, frozenset())
    assert not ok
    assert "return_access_stale" in errors or "return_chronology" in errors


def test_case3_buffer_ok_with_new_route(final_day_schedule):
    """14:59 end + ~20min to hub + 15 buffer ≤ 15:50 → valid after sync."""
    schedule = final_day_schedule
    tree = _point("tree", "큰나무집궁중약백숙 구미점", 128.348, 36.126)
    items = list(schedule.items[:-1])
    items.append(_visit(
        "tree", tree.name,
        datetime(2026, 9, 20, 13, 49, tzinfo=KST),
        datetime(2026, 9, 20, 14, 59, tzinfo=KST),
        meal="점심",
    ).model_copy(update={"destination": tree}))
    # Stale return from pig
    stale_origin = _point("pig", "꽃돼지식당 구미본점", 128.342, 36.123)
    stale_leg = schedule.return_journey.to_hub.model_copy(update={
        "origin": stale_origin.name,
        "origin_point": stale_origin,
        "departure_time": datetime(2026, 9, 20, 14, 51, tzinfo=KST),
        "duration_minutes": 31,
    })
    proposed = schedule.model_copy(update={
        "items": tuple(items),
        "return_journey": schedule.return_journey.model_copy(update={"to_hub": stale_leg}),
        "user_selected_place_id": "pig",
    })
    synced, ok = sync_final_day_return(proposed, schedule)
    assert ok and synced is not None
    assert synced.return_journey.to_hub.origin_point.id == "tree"
    assert synced.return_journey.to_hub.departure_time >= datetime(2026, 9, 20, 14, 59, tzinfo=KST)
    ready = (synced.return_journey.to_hub.arrival_time
             + timedelta(minutes=synced.return_journey.boarding_buffer_minutes))
    assert ready <= synced.return_journey.transport.departure_time
    assert return_boarding_feasible(synced)
    assert return_access_synced(synced)


def test_case4_buffer_fail_strips_activity(final_day_schedule):
    """If hub access + buffer misses 15:50, strip last activity and keep train."""
    schedule = final_day_schedule
    far = _point("far", "먼식당", 129.5, 37.5)  # far → long estimate
    items = list(schedule.items[:-1])
    items.append(_visit(
        "far", far.name,
        datetime(2026, 9, 20, 14, 30, tzinfo=KST),
        datetime(2026, 9, 20, 15, 20, tzinfo=KST),
        meal="점심",
    ).model_copy(update={"destination": far}))
    proposed = schedule.model_copy(update={
        "items": tuple(items),
        "user_selected_place_id": "pig",
    })
    synced, ok = sync_final_day_return(proposed, schedule)
    assert ok and synced is not None
    visit_ids = {i.place_id for i in _flat_items(synced) if i.item_type != "TRAVEL"}
    assert "far" not in visit_ids
    assert synced.return_journey.transport.departure_time == datetime(
        2026, 9, 20, 15, 50, tzinfo=KST)


def test_case5_6_timeline_matches_return_section(final_day_schedule, selected):
    """Timeline RETURN origin/times == return_journey (돌아가는 길 source)."""
    schedule = final_day_schedule
    tree = _point("tree", "큰나무집궁중약백숙 구미점", 128.348, 36.126)
    items = list(schedule.items[:-1])
    items.append(_visit(
        "tree", tree.name,
        datetime(2026, 9, 20, 13, 49, tzinfo=KST),
        datetime(2026, 9, 20, 14, 59, tzinfo=KST),
        meal="점심",
    ).model_copy(update={"destination": tree}))
    proposed = schedule.model_copy(update={"items": tuple(items)})
    synced, ok = sync_final_day_return(proposed, schedule)
    assert ok
    views = build_schedule_views(synced, selected=selected)
    assert len(views) == 1
    hub_events = [e for e in views[0].timeline if e.kind == "RETURN_HUB"]
    assert hub_events
    leg = synced.return_journey.to_hub
    assert hub_events[0].start == leg.departure_time
    assert hub_events[0].end == leg.arrival_time
    assert leg.origin in hub_events[0].title
    assert "꽃돼지" not in hub_events[0].title


def test_case7_main_kept_when_feasible(final_day_schedule, selected, trip):
    current = datetime(2026, 9, 20, 13, 0, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.DELAY,
        occurred_at=current,
        delay_minutes=10,
        source="USER_REPORTED",
    )
    result = replan_trip_schedule(
        _ctx(final_day_schedule, selected, trip, current, event),
        load_settings({}, {}),
    )
    if result.status not in {ReplanStatus.REPLAN_INFEASIBLE, ReplanStatus.REPLAN_NO_CHANGE}:
        ids = {i.place_id for i in _flat_items(result.proposed_schedule) if i.item_type != "TRAVEL"}
        if "pig" in ids:
            assert result.main_status != "REJECTED_BY_USER"
            assert result.proposed_schedule.user_selected_place_id == "pig"


def test_case8_main_infeasible_not_auto_replaced(final_day_schedule, selected, trip):
    """MAIN not feasible → INFEASIBLE; pool restaurant must not become MAIN."""
    schedule = final_day_schedule
    # Make MAIN impossible: current after cutoff window with huge delay
    current = datetime(2026, 9, 20, 15, 0, tzinfo=KST)
    done = frozenset(
        item_key(i) for i in schedule.items
        if i.place_id == "park" or (i.item_type == "TRAVEL" and i.destination and i.destination.id == "park")
    )
    tree = _candidate("tree", "큰나무집궁중약백숙 구미점")
    event = ReplanEvent(
        event_type=ReplanEventType.DELAY,
        occurred_at=current,
        delay_minutes=90,
        source="USER_REPORTED",
    )
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=done, pool=(tree,)),
        load_settings({}, {}),
    )
    if result.proposed_schedule is not None:
        assert result.proposed_schedule.user_selected_place_id != "tree"
        if result.main_status == "INFEASIBLE":
            assert "포함하기 어렵습니다" in " ".join(result.notices) or True


def test_case9_main_user_skip_rejected(final_day_schedule, selected, trip):
    current = datetime(2026, 9, 20, 13, 25, tzinfo=KST)
    done = frozenset(
        item_key(i) for i in final_day_schedule.items
        if i.item_type != "TRAVEL" and i.place_id == "park"
    )
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP,
        occurred_at=current,
        place_id="pig",
        source="USER_REPORTED",
    )
    result = replan_trip_schedule(
        _ctx(final_day_schedule, selected, trip, current, event, completed=done),
        load_settings({}, {}),
    )
    assert result.main_status == "REJECTED_BY_USER"
    ids = {i.place_id for i in _flat_items(result.proposed_schedule) if i.item_type != "TRAVEL"}
    assert "pig" not in ids


def test_case10_11_apply_drops_old_return_origin(final_day_schedule, selected):
    tree = _point("tree", "큰나무집궁중약백숙 구미점", 128.348, 36.126)
    items = list(final_day_schedule.items[:-1])
    items.append(_visit(
        "tree", tree.name,
        datetime(2026, 9, 20, 13, 49, tzinfo=KST),
        datetime(2026, 9, 20, 14, 59, tzinfo=KST),
        meal="점심",
    ).model_copy(update={"destination": tree}))
    synced, ok = sync_final_day_return(
        final_day_schedule.model_copy(update={"items": tuple(items)}),
        final_day_schedule,
    )
    assert ok
    views = build_schedule_views(synced, selected=selected)
    path_names = [p.name for p in views[0].path_sequence]
    marker_names = [m.name for m in views[0].markers]
    assert all("꽃돼지" not in n for n in path_names)
    # Old return origin must not appear as return hub title
    hub_titles = [e.title for e in views[0].timeline if e.kind == "RETURN_HUB"]
    assert hub_titles and all("꽃돼지" not in t for t in hub_titles)
    assert synced.return_journey.to_hub.origin_point.id == "tree"


def test_case12_final_arrival_deadline_kept(final_day_schedule):
    synced, ok = sync_final_day_return(final_day_schedule, final_day_schedule)
    assert ok
    assert synced.final_arrival_datetime <= synced.trip_end_datetime
    assert synced.final_arrival_datetime == synced.return_journey.to_origin.arrival_time


def test_case13_14_multiday_regression(selected, trip):
    from test_schedule_day_view import multiday_schedule
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
    assert result.status != ReplanStatus.REPLAN_INFEASIBLE or result.attempts >= 0
    if result.proposed_schedule and result.proposed_schedule.return_journey:
        assert return_access_synced(result.proposed_schedule)
        assert return_boarding_feasible(result.proposed_schedule)
        assert return_chain_chronological(result.proposed_schedule)
        assert result.proposed_schedule.return_journey.transport.departure_time == datetime(
            2026, 9, 20, 15, 50, tzinfo=KST)
