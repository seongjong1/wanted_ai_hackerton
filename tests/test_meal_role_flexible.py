"""Phase 4.7 — MAIN vs MealRole (FLEXIBLE lunch/dinner) regressions."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest.mock import Mock

import pytest

from config import ScheduleSettings
from dataclasses import replace

from models.access import AccessPoint
from models.place import PlaceCandidate
from models.transport import KST
from models.trip_request import Preference
from services.meal_role import MealRole, resolve_meal_role, is_restaurant
from test_places import trip, selected, anchor
from test_schedule import scheduler, candidates


def restaurant(place_id="main-meal", name="메인 식당", score=99):
    return PlaceCandidate(
        place_id=place_id, place_name=name, latitude=36.12, longitude=128.33,
        category="음식점 > 한식", preference_score=score,
        matched_preferences=(Preference.FOOD,), role="MAIN_DESTINATION", address="검증")


def tourism(place_id="main-tour", name="메인 관광지", score=99):
    return PlaceCandidate(
        place_id=place_id, place_name=name, latitude=36.121, longitude=128.331,
        category="여행 > 관광명소", preference_score=score,
        matched_preferences=(Preference.SIGHTSEEING,), role="MAIN_DESTINATION", address="검증")


def test_resolve_meal_role_defaults():
    assert resolve_meal_role(restaurant()) == MealRole.FLEXIBLE
    assert resolve_meal_role(tourism()) == MealRole.NONE
    assert resolve_meal_role(restaurant(), "저녁") == MealRole.DINNER
    assert resolve_meal_role(restaurant(), MealRole.LUNCH) == MealRole.LUNCH
    assert is_restaurant(restaurant()) and not is_restaurant(tourism())


def test_case1_flexible_picks_better_slot(trip, selected, anchor):
    """Both lunch and dinner possible → schedule quality chooses a meal slot."""
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={
        "arrival_time": selected.arrival_time.replace(hour=10, minute=0)})
    main = restaurant()
    other = candidates(meal=True, count=3)
    service, transit = scheduler(anchor, config=replace(ScheduleSettings(), late_lunch_end_hour=16))
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    result = service.generate(trip, selected, [main, *other], main.place_id)
    assert result.schedule
    assert result.schedule.anchor_status == "INCLUDED"
    assert result.schedule.anchor_meal_role in {"LUNCH", "DINNER"}
    meals = [i for i in result.schedule.items if i.place_id == main.place_id and i.item_type == "MEAL"]
    assert len(meals) == 1
    assert "영업시간 확인 필요" in meals[0].checks
    assert meals[0].open_status == "UNKNOWN"


def test_case2_dinner_when_lunch_impossible(trip, selected, anchor):
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    # Arrive after late lunch window ends.
    selected = selected.model_copy(update={
        "arrival_time": selected.arrival_time.replace(hour=16, minute=30)})
    main = restaurant()
    service, transit = scheduler(anchor, config=replace(ScheduleSettings(), late_lunch_end_hour=16))
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    result = service.generate(trip, selected, [main], main.place_id)
    assert result.schedule and result.schedule.anchor_status == "INCLUDED"
    meal = next(i for i in result.schedule.items if i.place_id == main.place_id and i.item_type != "TRAVEL")
    assert meal.meal_slot.endswith("저녁")
    assert result.schedule.anchor_meal_role == "DINNER"


def test_case3_lunch_when_forced_or_only_feasible(trip, selected, anchor):
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={
        "arrival_time": selected.arrival_time.replace(hour=11, minute=0)})
    main = restaurant()
    service, transit = scheduler(anchor)
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    result = service.generate(trip, selected, [main], main.place_id, preferred_meal_role=MealRole.LUNCH)
    assert result.schedule.anchor_status == "INCLUDED"
    assert result.schedule.anchor_meal_role == "LUNCH"
    meal = next(i for i in result.schedule.items if i.place_id == main.place_id and i.item_type != "TRAVEL")
    assert meal.meal_slot.endswith("점심")


def test_case4_infeasible_does_not_force(trip, selected, anchor):
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    main = restaurant()
    service, transit = scheduler(anchor)
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = (
        lambda a, b: route.model_copy(update={"duration_seconds": 50000})
        if b.id == main.place_id else route)
    result = service.generate(trip, selected, [main, *candidates(meal=True, count=2)], main.place_id)
    assert all(i.place_id != main.place_id for i in (result.schedule.items if result.schedule else ()))
    assert any("포함하기 어렵습니다" in n for n in result.notices)
    if result.schedule:
        assert result.schedule.anchor_status == "INFEASIBLE"


def test_case5_unknown_hours_still_flexible(trip, selected, anchor):
    """UNKNOWN opening hours are not treated as closed for lunch/dinner."""
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={
        "arrival_time": selected.arrival_time.replace(hour=12, minute=0)})
    main = restaurant()
    service, _ = scheduler(anchor)
    result = service.generate(trip, selected, [main], main.place_id)
    assert result.schedule.anchor_status == "INCLUDED"
    meal = next(i for i in result.schedule.items if i.place_id == main.place_id and i.item_type == "MEAL")
    assert meal.open_status == "UNKNOWN"
    assert "영업시간 확인 필요" in meal.checks


def test_case6_tourism_main_skips_meal_role(trip, selected, anchor):
    trip = trip.model_copy(update={"preferences": (Preference.SIGHTSEEING,)})
    main = tourism()
    extras = candidates(count=3)
    service, _ = scheduler(anchor)
    result = service.generate(trip, selected, [main, *extras], main.place_id)
    assert result.schedule.anchor_status == "INCLUDED"
    assert result.schedule.anchor_meal_role == "NONE"
    visit = next(i for i in result.schedule.items if i.place_id == main.place_id and i.item_type != "TRAVEL")
    assert visit.item_type == "PLACE"
    assert visit.meal_slot is None


def test_case8_exactly_once(trip, selected, anchor):
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    main = restaurant()
    service, _ = scheduler(anchor)
    result = service.generate(trip, selected, [main, *candidates(meal=True, count=4)], main.place_id)
    ids = [i.place_id for i in result.schedule.items if i.item_type != "TRAVEL"]
    assert ids.count(main.place_id) == 1


def test_case10_return_cutoff_prefers_lunch(trip, selected, anchor):
    """Dinner would miss a tight cutoff; lunch remains feasible → LUNCH."""
    trip = trip.model_copy(update={
        "preferences": (Preference.FOOD,),
        "end_time": time(15, 0),
    })
    selected = selected.model_copy(update={
        "arrival_time": selected.arrival_time.replace(hour=11, minute=0)})
    main = restaurant()
    service, transit = scheduler(anchor)
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    deadline = datetime.combine(trip.start_date, time(15, 0), KST)
    start = selected.arrival_time
    result = service.generate_window(
        trip, selected, [main], start=start, deadline=deadline,
        start_point=anchor, preferred_place_id=main.place_id, end_point=None)
    assert result.schedule
    assert result.schedule.anchor_status == "INCLUDED"
    assert result.schedule.anchor_meal_role == "LUNCH"
    meal = next(i for i in result.schedule.items if i.place_id == main.place_id and i.item_type != "TRAVEL")
    assert meal.end_datetime <= deadline


def test_case11_all_slots_miss_cutoff_infeasible(trip, selected, anchor):
    """Both lunch and dinner would miss a very early cutoff → INFEASIBLE."""
    trip = trip.model_copy(update={
        "preferences": (Preference.FOOD,),
        "end_time": time(11, 30),
    })
    selected = selected.model_copy(update={
        "arrival_time": selected.arrival_time.replace(hour=10, minute=0)})
    main = restaurant()
    service, transit = scheduler(anchor)
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    deadline = datetime.combine(trip.start_date, time(11, 30), KST)
    result = service.generate_window(
        trip, selected, [main, *candidates(meal=True, count=2)],
        start=selected.arrival_time, deadline=deadline,
        start_point=anchor, preferred_place_id=main.place_id)
    assert all(i.place_id != main.place_id for i in (result.schedule.items if result.schedule else ()))
    assert any("포함하기 어렵습니다" in n for n in result.notices)
    if result.schedule:
        assert result.schedule.anchor_status == "INFEASIBLE"


def test_case7_multiday_may_place_main_on_day2(trip, selected, anchor):
    """MAIN unreachable from arrival hub, reachable from lodging → DAY 2."""
    from test_multiday_schedule import multiday_service, multiday_trip, pool, lodging
    from providers.http_client import ProviderError

    request = multiday_trip(trip).model_copy(update={
        "preferences": (Preference.FOOD, Preference.SIGHTSEEING)})
    selected = selected.model_copy(update={
        "arrival_time": selected.arrival_time.replace(hour=11, minute=0)})
    stay = lodging()
    main = PlaceCandidate(
        place_id="main-meal", place_name="메인 식당",
        latitude=stay.y, longitude=stay.x,
        category="음식점 > 한식", preference_score=99,
        matched_preferences=(Preference.FOOD,), role="MAIN_DESTINATION", address="검증")
    extras = [c for c in pool(12) if c.place_id != main.place_id]
    service, *_ = multiday_service(request, selected, anchor)
    transit = service.activity.transit
    base = transit.fastest_route.return_value

    def route_for(a, b):
        # Only lodging can reach MAIN; DAY-1 hub/elsewhere hops fail → probe picks DAY 2.
        if b.id == main.place_id and a.id != stay.id:
            raise ProviderError("ROUTE_UNAVAILABLE")
        if a.id == main.place_id and b.id != stay.id and b.id != main.place_id:
            # Leaving MAIN toward non-lodging is fine for scoring; keep short hops.
            pass
        return base.model_copy(update={"duration_seconds": 400})

    transit.fastest_route.side_effect = route_for
    result = service.generate(request, selected, [main, *extras], main.place_id, stay.name)
    assert result.schedule and result.schedule.anchor_status == "INCLUDED"
    days_with_main = [
        d.day_index for d in result.schedule.days
        if any(i.place_id == main.place_id and i.item_type != "TRAVEL" for i in d.items)]
    assert days_with_main == [2]
    assert result.schedule.anchor_meal_role in {"LUNCH", "DINNER"}


def test_case9_main_not_repeated_across_days(trip, selected, anchor):
    from test_multiday_schedule import multiday_service, multiday_trip, pool

    request = multiday_trip(trip).model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={
        "arrival_time": selected.arrival_time.replace(hour=11, minute=0)})
    main = restaurant(place_id="main-once", name="한번만 식당", score=99)
    service, *_ = multiday_service(request, selected, anchor)
    result = service.generate(
        request, selected, [main, *pool(10)], main.place_id, "구미 테스트 숙소")
    assert result.schedule
    visits = [i.place_id for d in result.schedule.days for i in d.items if i.item_type != "TRAVEL"]
    assert visits.count(main.place_id) == 1
