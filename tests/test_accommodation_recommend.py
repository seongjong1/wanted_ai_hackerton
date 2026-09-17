"""Phase 4.8 — lodging candidate recommend / confirm / provisional fallback."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest.mock import Mock

import pytest

from config import ScheduleSettings, Settings
from models.access import AccessPoint, AccessRoute, AccessStep
from models.accommodation import AccommodationCandidate
from models.place import PlaceCandidate
from models.transport import KST
from models.trip_request import Preference
from providers.http_client import ProviderError
from services.accommodation_recommend_service import (
    AccommodationRecommendService, is_lodging_category, search_accommodation_candidates)
from services.multiday_schedule_service import PROVISIONAL_NOTICE
from test_multiday_schedule import lodging, multiday_service, multiday_trip, pool
from test_places import trip, selected, anchor


def lodging_row(place_id, name, x, y, category="여행 > 숙박 > 호텔", address="경북 구미시 원평동 1"):
    return {
        "id": place_id, "place_name": name, "x": str(x), "y": str(y),
        "category_name": category, "address_name": address, "road_address_name": address,
    }


def test_is_lodging_category_filters_non_hotels():
    assert is_lodging_category("여행 > 숙박 > 호텔")
    assert is_lodging_category("가정,생활 > 숙박 > 모텔")
    assert not is_lodging_category("음식점 > 한식")
    assert not is_lodging_category("여행 > 관광명소")


def test_case1_2_3_search_real_kakao_rows_and_limit(trip, selected, anchor):
    request = multiday_trip(trip)
    kakao = Mock()
    kakao.search_places.side_effect = lambda query, **kwargs: [
        lodging_row(f"h{i}", f"호텔{i}", 128.33 + i * 0.01, 36.12 + i * 0.01)
        for i in range(8)
    ] + [lodging_row("food", "가짜식당", 128.33, 36.12, category="음식점 > 한식")]
    transit = Mock()
    route = AccessRoute(
        duration_seconds=600, distance_meters=800, transfers=0,
        steps=(AccessStep(mode="WALKING", duration_seconds=600, distance_meters=800),))
    transit.fastest_route.return_value = route
    access = Mock()
    access.resolve_point.return_value = anchor
    config = ScheduleSettings(
        accommodation_prefilter_limit=5,
        accommodation_route_eval_limit=5,
        accommodation_recommend_limit=3,
        accommodation_search_queries=("호텔",),
    )
    service = AccommodationRecommendService(kakao, transit, access, config)
    result = service.search(request, selected)
    assert result.status == "ok"
    assert len(result.candidates) == 3
    assert all(is_lodging_category(c.category) for c in result.candidates)
    assert all(c.place_id.startswith("h") for c in result.candidates)
    assert result.route_eval_count <= config.accommodation_route_eval_limit
    assert "예약 가능한 숙소" not in " ".join(result.notices)
    assert "객실 가격과 예약 가능 여부" in " ".join(result.notices)


def test_case4_ranking_prefers_closer_hub_route(trip, selected, anchor):
    request = multiday_trip(trip)
    kakao = Mock()
    kakao.search_places.return_value = [
        lodging_row("near", "가까운호텔", 128.331, 36.121),
        lodging_row("far", "먼호텔", 128.45, 36.25),
    ]
    transit = Mock()

    def route_for(a, b):
        seconds = 300 if b.id == "near" else 2400
        return AccessRoute(
            duration_seconds=seconds, distance_meters=seconds, transfers=0,
            steps=(AccessStep(mode="WALKING", duration_seconds=seconds, distance_meters=seconds),))

    transit.fastest_route.side_effect = route_for
    access = Mock()
    access.resolve_point.return_value = anchor
    service = AccommodationRecommendService(
        kakao, transit, access, ScheduleSettings(accommodation_search_queries=("호텔",)))
    result = service.search(request, selected)
    assert result.candidates[0].place_id == "near"
    assert "접근" in result.candidates[0].reason or "이동" in result.candidates[0].reason
    assert "가성비" not in result.candidates[0].reason


def test_case5_8_select_confirmed_day_boundaries(trip, selected, anchor):
    request = multiday_trip(trip)
    stay = lodging()
    service, *_ = multiday_service(request, selected, anchor)
    result = service.generate(
        request, selected, pool(), None, accommodation_undecided=False,
        accommodation_point=stay)
    assert result.schedule
    assert result.schedule.accommodation_status == "CONFIRMED"
    assert result.schedule.accommodation.id == stay.id
    assert result.schedule.days[0].end_location.id == stay.id
    assert result.schedule.days[1].start_location.id == stay.id
    assert result.schedule.days[1].end_location.id == stay.id
    assert result.schedule.days[2].start_location.id == stay.id
    assert result.schedule.return_status.value.startswith("RETURN_")


def test_case11_provisional_without_selection(trip, selected, anchor):
    request = multiday_trip(trip)
    service, *_ = multiday_service(request, selected, anchor)
    result = service.generate(
        request, selected, pool(), None, accommodation_undecided=True)
    assert result.schedule.accommodation_status == "PROVISIONAL"
    assert result.schedule.accommodation.id == anchor.id or result.schedule.accommodation.name
    assert any("임시 기준점" in n for n in result.notices) or PROVISIONAL_NOTICE in result.notices


def test_case13_no_price_rating_fields():
    candidate = AccommodationCandidate(
        place_id="1", place_name="호텔", address="경북 구미시", category="숙박 > 호텔",
        latitude=36.12, longitude=128.33, reason="이동 거리가 짧은 후보입니다.")
    dumped = candidate.model_dump()
    assert "price" not in dumped and "rating" not in dumped and "review" not in dumped
    assert "room" not in dumped and "score" in dumped  # internal ranking only


def test_case14_search_failure_does_not_raise(trip, selected, anchor, monkeypatch):
    request = multiday_trip(trip)
    settings = Settings(kakao_rest_api_key="x")
    monkeypatch.setattr(
        "services.accommodation_recommend_service.KakaoProvider",
        Mock(side_effect=ProviderError("quota")))
    result = search_accommodation_candidates(request, selected, settings)
    assert result.status == "failed"
    assert "숙소 후보를 불러오지 못했습니다" in result.notices[0]


def test_case16_main_anchor_independent_of_lodging(trip, selected, anchor):
    request = multiday_trip(trip)
    stay = lodging()
    main = PlaceCandidate(
        place_id="main-food", place_name="메인식당", latitude=36.12, longitude=128.33,
        category="음식점 > 한식", preference_score=99,
        matched_preferences=(Preference.FOOD,), role="MAIN_DESTINATION")
    service, *_ = multiday_service(request, selected, anchor)
    result = service.generate(
        request, selected, [main, *pool(8)], main.place_id,
        accommodation_point=stay)
    assert result.schedule.user_selected_place_id == main.place_id
    assert result.schedule.accommodation.id == stay.id
    visits = [i.place_id for d in result.schedule.days for i in d.items if i.item_type != "TRAVEL"]
    assert visits.count(main.place_id) == 1


def test_case18_return_respects_end_datetime(trip, selected, anchor):
    request = multiday_trip(trip)
    stay = lodging()
    service, *_ = multiday_service(request, selected, anchor)
    result = service.generate(request, selected, pool(), None, accommodation_point=stay)
    assert result.schedule.final_arrival_datetime is None or (
        result.schedule.final_arrival_datetime
        <= datetime.combine(request.end_date, request.end_time, KST))


def test_case20_route_eval_capped(trip, selected, anchor):
    request = multiday_trip(trip)
    kakao = Mock()
    kakao.search_places.return_value = [
        lodging_row(f"h{i}", f"호텔{i}", 128.33 + i * 0.002, 36.12) for i in range(12)]
    transit = Mock()
    transit.fastest_route.return_value = AccessRoute(
        duration_seconds=500, distance_meters=500, transfers=0,
        steps=(AccessStep(mode="WALKING", duration_seconds=500, distance_meters=500),))
    access = Mock()
    access.resolve_point.return_value = anchor
    config = ScheduleSettings(
        accommodation_search_queries=("호텔",),
        accommodation_prefilter_limit=5,
        accommodation_route_eval_limit=5,
        accommodation_recommend_limit=3)
    service = AccommodationRecommendService(kakao, transit, access, config)
    result = service.search(request, selected)
    assert transit.fastest_route.call_count <= config.accommodation_route_eval_limit
    assert len(result.candidates) == 3
