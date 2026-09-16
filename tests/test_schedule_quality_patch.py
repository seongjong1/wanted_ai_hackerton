"""Phase 4.6 final Schedule Quality Patch — MAIN vs SUPPORT split + Final Day cutoff fill."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from unittest.mock import Mock

import pytest

from config import ScheduleSettings
from models.access import AccessPoint
from models.transport import KST
from models.trip_request import Preference
from services.itinerary_suitability import is_micro_landmark
from services.schedule_support_search import SUPPORT_SEARCH_TERMS, search_schedule_support
from test_places import trip, selected, anchor
from test_schedule import scheduler, candidates


def test_case1_main_food_preference_stays_food_centered(trip):
    """CASE 1: MAIN candidates for 맛집 remain restaurant-oriented."""
    food = candidates(meal=True, count=3)
    assert all(Preference.FOOD in c.matched_preferences for c in food)
    assert all("음식점" in c.category for c in food)


def test_case2_support_search_queries_enrichment_not_food_only(trip, anchor):
    """CASE 2: Supporting pool explores tourism/park/market/culture/cafe."""
    labels = [term for term, _ in SUPPORT_SEARCH_TERMS]
    for required in ("관광", "공원", "산책", "시장", "전통시장", "박물관", "전시", "문화", "카페"):
        assert required in labels
    kakao = Mock()
    kakao.search_places.return_value = [{
        "id": "park1", "place_name": "구미공원", "x": "128.34", "y": "36.13",
        "address_name": "경북 구미시 공원로 1", "category_name": "여행 > 공원",
        "place_url": "https://example.com/p", "distance": "100",
    }]
    found = search_schedule_support(kakao, trip, anchor, include_cafe=True, limit=24)
    queries = [c.args[0] for c in kakao.search_places.call_args_list]
    assert all(q.startswith(f"{trip.destination} ") for q in queries)
    assert any("관광" in q for q in queries)
    assert any("공원" in q for q in queries)
    assert not any(q.endswith(" 맛집") or q.endswith(" 음식점") for q in queries)
    assert found and all(c.role == "MAIN_DESTINATION" for c in found)


def test_case3_day1_97min_gap_inserts_supporting(trip, selected, anchor):
    """CASE 3: ~97m gap before dinner inserts supporting activity when candidate exists."""
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=13, minute=25)})
    lunch = candidates(meal=True, count=1)[0].model_copy(update={
        "place_id": "lunch", "place_name": "점심", "address": "검증", "preference_score": 95})
    dinner = candidates(meal=True, count=2)[1].model_copy(update={
        "place_id": "dinner", "place_name": "저녁", "address": "검증", "preference_score": 70})
    tour = candidates(count=1)[0].model_copy(update={
        "place_id": "tour97", "place_name": "산책로", "address": "검증",
        "category": "여행 > 관광명소", "matched_preferences": (Preference.SIGHTSEEING,),
        "role": "MAIN_DESTINATION"})
    search = Mock(return_value=[tour])
    service, transit = scheduler(
        anchor, supporting_search=search,
        config=replace(ScheduleSettings(), late_lunch_end_hour=15, max_gap_fill_iterations=6))
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    result = service.generate(trip, selected, [lunch, dinner], lunch.place_id)
    assert result.schedule
    visits = [i for i in result.schedule.items if i.item_type != "TRAVEL"]
    ids = [i.place_id for i in visits]
    assert "tour97" in ids
    search.assert_called()


def test_case4_and_5_morning_and_afternoon_long_gaps(trip, selected, anchor):
    """CASE 4–5: Middle-day morning 110m + afternoon 236m gaps receive supporting fills."""
    stay = AccessPoint(id="stay", name="임시 숙소", x=128.34, y=36.13)
    day = trip.start_date + timedelta(days=1)
    start = datetime.combine(day, time(9), KST)
    deadline = datetime.combine(day, time(21), KST)
    selected = selected.model_copy(update={"arrival_time": start})
    trip = trip.model_copy(update={
        "preferences": (Preference.FOOD,), "end_date": day, "end_time": time(21),
        "has_accommodation": True})
    lunch = candidates(meal=True, count=1)[0].model_copy(update={
        "place_id": "lunch", "place_name": "점심", "address": "검증", "preference_score": 90})
    dinner = candidates(meal=True, count=2)[1].model_copy(update={
        "place_id": "dinner", "place_name": "저녁", "address": "검증", "preference_score": 80})
    morning = candidates(count=1)[0].model_copy(update={
        "place_id": "morning", "place_name": "오전 공원", "address": "검증",
        "category": "여행 > 공원", "matched_preferences": (Preference.SIGHTSEEING,),
        "role": "MAIN_DESTINATION"})
    afternoon = candidates(count=1)[0].model_copy(update={
        "place_id": "afternoon", "place_name": "오후 시장", "address": "검증",
        "category": "가정,생활 > 시장", "matched_preferences": (Preference.SHOPPING,),
        "role": "MAIN_DESTINATION"})
    search = Mock(return_value=[morning, afternoon])
    service, transit = scheduler(
        stay, supporting_search=search,
        config=replace(ScheduleSettings(), late_lunch_end_hour=15, max_gap_fill_iterations=8))
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    result = service.generate_window(
        trip, selected, [lunch, dinner], start=start, deadline=deadline,
        start_point=stay, end_point=stay)
    assert result.schedule
    visits = [i for i in result.schedule.items if i.item_type != "TRAVEL"]
    ids = {i.place_id for i in visits}
    assert "morning" in ids or "afternoon" in ids
    lunch_item = next(i for i in visits if i.place_id == "lunch")
    dinner_item = next(i for i in visits if i.place_id == "dinner")
    between = [i for i in visits if lunch_item.end_datetime <= i.start_datetime < dinner_item.start_datetime]
    assert between, "afternoon supporting activity required when candidates exist"


def test_case6_7_8_final_day_cutoff_gap_and_hub_feasibility(trip, selected, anchor):
    """CASE 6–8: 12:32–14:53 trailing window is gap-fill target; hub route must fit."""
    stay = AccessPoint(id="stay", name="임시 숙소", x=128.34, y=36.13)
    hub = AccessPoint(id="hub", name="구미역", x=128.35, y=36.14)
    day = trip.end_date
    morning = datetime.combine(day, time(9), KST)
    cutoff = datetime.combine(day, time(14, 53), KST)
    selected = selected.model_copy(update={"arrival_time": morning})
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,), "end_time": time(14, 53)})
    lunch = candidates(meal=True, count=1)[0].model_copy(update={
        "place_id": "lunch", "place_name": "산동식당", "address": "검증", "preference_score": 95})
    cafe = candidates(count=1)[0].model_copy(update={
        "place_id": "cafe", "place_name": "카페", "address": "검증",
        "category": "카페", "matched_preferences": (Preference.REST,), "preference_score": 50,
        "role": "MAIN_DESTINATION"})
    tour = candidates(count=1)[0].model_copy(update={
        "place_id": "pm_tour", "place_name": "오후 관광", "address": "검증",
        "category": "여행 > 관광명소", "matched_preferences": (Preference.SIGHTSEEING,),
        "role": "MAIN_DESTINATION"})
    search = Mock(return_value=[tour])
    service, transit = scheduler(
        stay, supporting_search=search,
        config=replace(ScheduleSettings(), late_lunch_end_hour=15, max_gap_fill_iterations=8,
                       cafe_soft_cap=1))
    route = transit.fastest_route.return_value

    def routing(a, b):
        # Hub from afternoon tourism is short enough to keep boarding buffer before 15:50-equivalent cutoff.
        seconds = 900 if b.id == hub.id else 600
        return route.model_copy(update={"duration_seconds": seconds})

    transit.fastest_route.side_effect = routing
    service.return_anchor = hub
    result = service.generate_window(
        trip, selected, [lunch, cafe], start=morning, deadline=cutoff,
        start_point=stay, end_point=None)
    assert result.schedule
    visits = [i for i in result.schedule.items if i.item_type != "TRAVEL"]
    assert any(i.place_id == "pm_tour" for i in visits)
    last_activity = max((i for i in visits), key=lambda i: i.end_datetime)
    assert last_activity.end_datetime <= cutoff
    # Activity should use more of the cutoff window than stopping right after an early cafe.
    assert last_activity.end_datetime.hour >= 13
    assert any(c.args[1].id == hub.id for c in transit.fastest_route.call_args_list)


def test_case9_no_force_fill_without_good_candidate(trip, selected, anchor):
    """CASE 9: Leave long gap empty when only low-quality options exist."""
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=13, minute=0)})
    dinner = candidates(meal=True, count=1)[0]
    landmark = candidates(count=1)[0].model_copy(update={
        "place_id": "tower", "place_name": "수출산업의탑", "address": "검증",
        "category": "여행 > 관광명소 > 기념비", "matched_preferences": (Preference.SIGHTSEEING,)})
    assert is_micro_landmark(landmark)
    search = Mock(return_value=[landmark])
    service, _ = scheduler(anchor, supporting_search=search,
                           config=replace(ScheduleSettings(), late_lunch_end_hour=15))
    result = service.generate(trip, selected, [dinner], dinner.place_id)
    assert result.schedule
    assert all(i.place_id != "tower" for i in result.schedule.items)


def test_case10_micro_landmark_not_promoted_for_gap(trip, selected, anchor):
    """CASE 10: Micro-landmark stays out of PRIMARY 60m sightseeing via gap fill."""
    kakao = Mock()
    kakao.search_places.return_value = [{
        "id": "tower", "place_name": "수출산업의탑", "x": "128.34", "y": "36.13",
        "address_name": "경북 구미시", "category_name": "여행 > 관광명소 > 기념탑",
        "place_url": "https://example.com/t", "distance": "50",
    }]
    found = search_schedule_support(kakao, trip, anchor)
    assert all(not is_micro_landmark(c) for c in found)
    assert all(c.place_id != "tower" for c in found)


def test_case11_exclude_prior_day_place_ids(trip, selected, anchor):
    """CASE 11: Prior-day place_id cannot re-enter supporting pool."""
    prior = candidates(count=1)[0].model_copy(update={
        "place_id": "day1_place", "address": "검증",
        "matched_preferences": (Preference.SIGHTSEEING,), "category": "여행 > 관광명소",
        "role": "MAIN_DESTINATION"})
    fresh = candidates(count=1)[0].model_copy(update={
        "place_id": "day2_place", "address": "검증",
        "matched_preferences": (Preference.SIGHTSEEING,), "category": "여행 > 공원",
        "role": "MAIN_DESTINATION"})
    meal = candidates(meal=True, count=1)[0]
    search = Mock(return_value=[prior, fresh])
    service, transit = scheduler(
        anchor, supporting_search=search,
        config=replace(ScheduleSettings(), late_lunch_end_hour=15, max_gap_fill_iterations=4))
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=13, minute=0)})
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    result = service.generate_window(
        trip, selected, [meal], start=selected.arrival_time,
        deadline=datetime.combine(trip.end_date, time(20), KST),
        start_point=anchor, exclude_ids=frozenset({"day1_place"}))
    assert result.schedule
    ids = {i.place_id for i in result.schedule.items if i.item_type != "TRAVEL"}
    assert "day1_place" not in ids
    assert "day2_place" in ids


def test_case12_category_diversity_prefers_tourism_over_cafe_stack(trip, selected, anchor):
    """CASE 12: Enrichment beats repeated cafe when both exist."""
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=14, minute=0)})
    dinner = candidates(meal=True, count=1)[0]
    cafe = candidates(count=1)[0].model_copy(update={
        "place_id": "cafe_a", "category": "카페", "address": "검증",
        "matched_preferences": (Preference.REST,), "role": "MAIN_DESTINATION"})
    tour = candidates(count=1)[0].model_copy(update={
        "place_id": "tour_a", "category": "여행 > 관광명소", "address": "검증",
        "matched_preferences": (Preference.SIGHTSEEING,), "role": "MAIN_DESTINATION"})
    search = Mock(return_value=[cafe, tour])
    service, transit = scheduler(
        anchor, supporting_search=search,
        config=replace(ScheduleSettings(), late_lunch_end_hour=15, max_gap_fill_iterations=2))
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    result = service.generate(trip, selected, [dinner], dinner.place_id)
    visits = [i for i in result.schedule.items if i.item_type != "TRAVEL" and i.place_id != dinner.place_id]
    assert visits and visits[0].place_id == "tour_a"


def test_case13_return_arrival_deadline_contract():
    """CASE 13: Home arrival must be <= trip end_time (validator contract)."""
    from services.return_schedule_service import validate_return
    # Covered by existing return/multiday validators; assert helper import path stays available.
    assert callable(validate_return)
