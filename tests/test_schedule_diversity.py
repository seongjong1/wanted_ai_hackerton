"""Category diversity / cafe soft-cap for schedule quality."""
from datetime import datetime, timedelta, time
from dataclasses import replace
from unittest.mock import Mock

from config import ScheduleSettings
from models.access import AccessPoint, AccessRoute, AccessStep
from models.place import PlaceCandidate
from models.schedule import ScheduleItem
from models.transport import KST
from models.trip_request import Preference
from services.itinerary_suitability import (
    activity_bucket, allow_cafe_candidate, cafe_imbalanced, diversity_penalty, visited_buckets)
from services.schedule_service import ScheduleService
from test_places import trip, selected, anchor
from test_schedule import scheduler, candidates


def cafe(place_id, name="검증 카페"):
    return PlaceCandidate(
        place_id=place_id, place_name=name, latitude=36.12, longitude=128.33,
        category="음식점 > 카페", preference_score=90, address="경북 구미시",
        matched_preferences=(Preference.REST,), role="NEARBY_PLACE")


def tourism(place_id, name="검증 공원"):
    return PlaceCandidate(
        place_id=place_id, place_name=name, latitude=36.121, longitude=128.331,
        category="여행 > 관광,명소 > 공원", preference_score=70, address="경북 구미시",
        matched_preferences=(Preference.SIGHTSEEING,), role="NEARBY_PLACE")


def tower():
    return PlaceCandidate(
        place_id="tower", place_name="검증 기념탑", latitude=36.122, longitude=128.332,
        category="여행 > 관광,명소 > 문화유적 > 탑,비석", preference_score=95, address="경북 구미시",
        matched_preferences=(Preference.SIGHTSEEING,), role="MAIN_DESTINATION")


def test_cafe_triple_blocked_when_tourism_exists(trip):
    prior = ["MEAL", "CAFE", "CAFE"]
    config = ScheduleSettings()
    assert not allow_cafe_candidate(cafe("c3"), trip, prior, config, alternatives_exist=True)
    assert not allow_cafe_candidate(cafe("c2"), trip, ["MEAL", "CAFE"], config,
                                    alternatives_exist=False, for_gap_fill=True)
    assert allow_cafe_candidate(tourism("t1"), trip, prior, config, alternatives_exist=True)


def test_diversity_prefers_tourism_over_second_cafe(trip):
    prior = ["MEAL", "CAFE"]
    config = ScheduleSettings()
    cafe_pen = diversity_penalty(cafe("c2"), trip, prior, config)
    tour_pen = diversity_penalty(tourism("t1"), trip, prior, config)
    assert cafe_pen > tour_pen


def test_rest_preference_relaxes_cafe_cap(trip):
    request = trip.model_copy(update={"preferences": (Preference.REST, Preference.FOOD)})
    prior = ["MEAL", "CAFE"]
    assert allow_cafe_candidate(cafe("c2"), request, prior, ScheduleSettings(), alternatives_exist=True)


def test_micro_landmark_not_used_to_fix_cafe_stack(trip):
    # Even when cafes are stacked, SHORT_STOP towers stay ineligible for gap fill elsewhere;
    # diversity helpers should still mark imbalance.
    assert cafe_imbalanced(["MEAL", "CAFE", "CAFE", "CAFE", "MEAL"], trip, ScheduleSettings())
    assert activity_bucket(tower()) == "TOURISM" or activity_bucket(tower()) == "CULTURE"
    # tower category has 관광 → TOURISM bucket, but suitability filter still blocks scheduling.


def test_gap_fill_avoids_third_cafe(trip, selected, anchor):
    lunch = candidates(meal=True, count=1)[0].model_copy(update={
        "place_id": "lunch", "place_name": "점심", "preference_score": 90})
    dinner = candidates(meal=True, count=1)[0].model_copy(update={
        "place_id": "dinner", "place_name": "저녁", "preference_score": 80,
        "latitude": 36.125, "longitude": 128.335})
    c1, c2, c3 = cafe("c1", "카페1"), cafe("c2", "카페2"), cafe("c3", "카페3")
    park = tourism("park", "중앙공원")
    support = Mock(return_value=[c1, c2, c3, park])
    service, transit = scheduler(
        anchor, supporting_search=support,
        config=replace(ScheduleSettings(), min_supporting_activity_gap_minutes=60,
                       accommodation_return_buffer_minutes=15))
    transit.fastest_route.return_value = AccessRoute(
        duration_seconds=600, distance_meters=800, transfers=0,
        steps=(AccessStep(mode="WALKING", duration_seconds=600, distance_meters=800),))
    request = trip.model_copy(update={"preferences": (Preference.FOOD,), "end_time": time(21)})
    service._user_preferences = (Preference.FOOD,)
    start = datetime.combine(trip.start_date, time(11), KST)
    # Seed a lunch + two cafes already; afternoon gap before dinner travel.
    lunch_end = start + timedelta(hours=1)
    items = [
        ScheduleItem(item_type="MEAL", place_id="lunch", place_name="점심",
                     start_datetime=start, end_datetime=lunch_end, preference="맛집",
                     meal_slot=f"{trip.start_date}:점심", estimated_duration=True,
                     checks=("영업시간 확인 필요",)),
        ScheduleItem(item_type="TRAVEL", place_id="c1", place_name="카페1",
                     start_datetime=lunch_end, end_datetime=lunch_end + timedelta(minutes=10),
                     origin=AccessPoint(id="lunch", name="점심", x=128.33, y=36.12),
                     destination=AccessPoint(id="c1", name="카페1", x=128.33, y=36.12),
                     travel_mode=("WALKING",), travel_duration_minutes=10, reason="이동"),
        ScheduleItem(item_type="PLACE", place_id="c1", place_name="카페1",
                     start_datetime=lunch_end + timedelta(minutes=10),
                     end_datetime=lunch_end + timedelta(minutes=40), preference="휴식",
                     estimated_duration=True, checks=("영업시간 확인 필요",)),
        ScheduleItem(item_type="TRAVEL", place_id="c2", place_name="카페2",
                     start_datetime=lunch_end + timedelta(minutes=40),
                     end_datetime=lunch_end + timedelta(minutes=50),
                     origin=AccessPoint(id="c1", name="카페1", x=128.33, y=36.12),
                     destination=AccessPoint(id="c2", name="카페2", x=128.33, y=36.12),
                     travel_mode=("WALKING",), travel_duration_minutes=10, reason="이동"),
        ScheduleItem(item_type="PLACE", place_id="c2", place_name="카페2",
                     start_datetime=lunch_end + timedelta(minutes=50),
                     end_datetime=lunch_end + timedelta(minutes=80), preference="휴식",
                     estimated_duration=True, checks=("영업시간 확인 필요",)),
        ScheduleItem(item_type="TRAVEL", place_id="dinner", place_name="저녁",
                     start_datetime=datetime.combine(trip.start_date, time(16, 30), KST),
                     end_datetime=datetime.combine(trip.start_date, time(17), KST),
                     origin=AccessPoint(id="c2", name="카페2", x=128.33, y=36.12),
                     destination=AccessPoint(id="dinner", name="저녁", x=128.335, y=36.125),
                     travel_mode=("WALKING",), travel_duration_minutes=30, reason="이동"),
        ScheduleItem(item_type="MEAL", place_id="dinner", place_name="저녁",
                     start_datetime=datetime.combine(trip.start_date, time(17), KST),
                     end_datetime=datetime.combine(trip.start_date, time(18), KST),
                     preference="맛집", meal_slot=f"{trip.start_date}:저녁", estimated_duration=True,
                     checks=("영업시간 확인 필요",)),
    ]
    available = {p.place_id: p for p in (lunch, dinner, c1, c2)}
    filled, supporting = service._fill_gaps(
        request, selected.model_copy(update={"arrival_time": start}),
        anchor, items, {}, {}, available, frozenset())
    cafe_names = [i.place_name for i in filled if i.item_type != "TRAVEL" and "카페" in i.place_name]
    assert len(cafe_names) <= 2
    # Prefer park insertion over a third cafe when the long afternoon gap is filled.
    non_travel = [i.place_name for i in filled if i.item_type != "TRAVEL"]
    assert "카페3" not in non_travel


def test_cafe_imbalance_detection(trip):
    assert cafe_imbalanced(["MEAL", "CAFE", "CAFE", "CAFE", "MEAL"],
                           trip.model_copy(update={"preferences": (Preference.FOOD,)}),
                           ScheduleSettings())
    assert not cafe_imbalanced(["MEAL", "CAFE", "TOURISM", "MEAL"],
                               trip.model_copy(update={"preferences": (Preference.FOOD,)}),
                               ScheduleSettings())


def test_visited_buckets_order():
    known = {"a": cafe("a"), "b": tourism("b")}
    items = [
        ScheduleItem(item_type="PLACE", place_id="a", place_name="카페", start_datetime=datetime(2026, 10, 1, 12, tzinfo=KST),
                     end_datetime=datetime(2026, 10, 1, 12, 30, tzinfo=KST), estimated_duration=True,
                     preference="휴식", checks=("영업시간 확인 필요",)),
        ScheduleItem(item_type="PLACE", place_id="b", place_name="공원", start_datetime=datetime(2026, 10, 1, 13, tzinfo=KST),
                     end_datetime=datetime(2026, 10, 1, 14, tzinfo=KST), estimated_duration=True,
                     preference="관광", checks=("영업시간 확인 필요",)),
    ]
    assert visited_buckets(items, known) == ["CAFE", "WALK"]
