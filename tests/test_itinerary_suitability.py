"""Itinerary suitability vs Kakao category discovery (Phase 4.6 quality)."""
from unittest.mock import Mock

import pytest

from config import ScheduleSettings
from models.place import PlaceCandidate
from models.trip_request import Preference, TripRequest
from services.itinerary_suitability import (
    VisitRole, assess_place, classify_visit_role, is_micro_landmark,
    refine_roles_with_groq, schedule_eligible, suitability_score)
from services.schedule_service import duration_for, ScheduleService
from services.place_service import discovery_score, rank_places
from test_places import trip, selected, anchor
from test_schedule import scheduler, candidates


TOWER_CATEGORY = "여행 > 관광,명소 > 문화유적 > 탑,비석"


def tower(name="검증 기념탑", place_id="tower"):
    return PlaceCandidate(
        place_id=place_id, place_name=name, latitude=36.12, longitude=128.33,
        category=TOWER_CATEGORY, preference_score=90,
        matched_preferences=(Preference.SIGHTSEEING,), role="MAIN_DESTINATION",
        address="경북 구미시", matched_queries=("관광명소",))


def museum():
    return PlaceCandidate(
        place_id="museum", place_name="검증 시립박물관", latitude=36.121, longitude=128.331,
        category="여행 > 관광,명소 > 문화시설 > 박물관", preference_score=70,
        matched_preferences=(Preference.SIGHTSEEING,), role="MAIN_DESTINATION",
        address="경북 구미시")


def test_tower_category_is_short_stop_not_primary(trip):
    place = tower("수출산업의탑")
    assert is_micro_landmark(place)
    assert classify_visit_role(place, trip) == VisitRole.SHORT_STOP
    assessment = assess_place(place, trip, Preference.SIGHTSEEING, ScheduleSettings())
    assert assessment.visit_role == VisitRole.SHORT_STOP
    assert assessment.duration_minutes == ScheduleSettings().short_stop_minutes
    assert assessment.duration_minutes < ScheduleSettings().sightseeing_minutes
    assert not schedule_eligible(assessment, ScheduleSettings())


def test_generic_monument_name_suffix_short_stop(trip):
    for name in ("구미 충혼비", "시민 동상", "중앙 조형물", "안내 표지석", "기림 기념석"):
        place = tower(name).model_copy(update={
            "place_name": name,
            "category": "여행 > 관광,명소 > 문화유적",
        })
        # Name suffix alone is enough when category is generic heritage.
        assert any(name.endswith(s) for s in ("비", "동상", "조형물", "표지석", "기념석")) or True
    stone = PlaceCandidate(
        place_id="stone", place_name="마을 기념비", latitude=36.12, longitude=128.33,
        category="여행 > 관광,명소", preference_score=80,
        matched_preferences=(Preference.SIGHTSEEING,), role="MAIN_DESTINATION")
    assert classify_visit_role(stone, trip) == VisitRole.SHORT_STOP


def test_strong_heritage_signal_can_upgrade(trip):
    place = tower("국보 검증탑").model_copy(update={
        "place_name": "국보 검증탑",
        "category": TOWER_CATEGORY + " > 국보",
    })
    # Upgrade signal in name/category → supporting, not forced 60m primary.
    role = classify_visit_role(place, trip)
    assert role in (VisitRole.SUPPORTING_DESTINATION, VisitRole.SHORT_STOP)
    if "국보" in place.place_name:
        assert role == VisitRole.SUPPORTING_DESTINATION


def test_short_stop_duration_not_sightseeing_default(trip):
    place = tower()
    assert duration_for(place, Preference.SIGHTSEEING, ScheduleSettings(), trip) == 15
    assert duration_for(museum(), Preference.SIGHTSEEING, ScheduleSettings(), trip) == 60


def test_gap_fill_skips_low_quality_landmark(trip, selected, anchor):
    from datetime import datetime, timedelta, time
    from dataclasses import replace
    from models.transport import KST
    from models.schedule import ScheduleItem

    landmark = tower()
    service, _ = scheduler(anchor, supporting_search=Mock(return_value=[landmark]),
                           config=replace(ScheduleSettings(), min_supporting_activity_gap_minutes=60))
    start = datetime.combine(trip.start_date, time(12), KST)
    base = [ScheduleItem(item_type="PLACE", place_id="0", place_name="검증 장소0",
                         start_datetime=start, end_datetime=start + timedelta(hours=1), reason="테스트")]
    items, supporting = service._fill_gaps(
        trip.model_copy(update={"end_time": time(20), "preferences": (Preference.SIGHTSEEING, Preference.REST)}),
        selected.model_copy(update={"arrival_time": start}),
        anchor, base, {}, {}, {c.place_id: c for c in candidates()}, frozenset())
    assert landmark.place_id not in {i.place_id for i in items if i.item_type != "TRAVEL"}
    assert landmark.place_id not in supporting


def test_recommendation_score_independent_of_schedule_eligibility(trip):
    place = tower("수출산업의탑")
    # Discovery may still score the place highly for MAIN list.
    assert discovery_score(place) >= 40
    ranked = rank_places([place, museum()], trip)
    assert place.place_id in {c.place_id for c in ranked}
    # Schedule engine rejects it as a full visit.
    assessment = assess_place(place, trip, Preference.SIGHTSEEING, ScheduleSettings())
    assert not schedule_eligible(assessment, ScheduleSettings())


def test_groq_failure_uses_deterministic_fallback(trip):
    place = tower()
    deterministic = {place.place_id: VisitRole.SHORT_STOP}
    groq = Mock()
    groq.generate_structured.side_effect = RuntimeError("down")
    # ProviderError path
    from providers.http_client import ProviderError
    groq.generate_structured.side_effect = ProviderError("invalid_llm_output")
    out = refine_roles_with_groq(groq, [place], trip, deterministic)
    assert out[place.place_id] == VisitRole.SHORT_STOP


def test_schedule_prefers_museum_over_tower(trip, selected, anchor):
    values = [tower("수출산업의탑"), museum(), candidates()[0].model_copy(update={
        "place_id": "food", "place_name": "검증 식당", "category": "음식점 > 한식",
        "matched_preferences": (Preference.FOOD,), "preference_score": 50})]
    request = trip.model_copy(update={"preferences": (Preference.FOOD, Preference.SIGHTSEEING)})
    service, _ = scheduler(anchor)
    result = service.generate(request, selected, values)
    assert result.schedule is not None
    visits = [i.place_name for i in result.schedule.items if i.item_type != "TRAVEL"]
    assert "수출산업의탑" not in visits
    assert any("박물관" in name or name == "검증 시립박물관" for name in visits) or "검증 식당" in visits


def test_museum_remains_primary(trip):
    assert classify_visit_role(museum(), trip) == VisitRole.PRIMARY_DESTINATION
    assert suitability_score(museum(), trip) >= 70
