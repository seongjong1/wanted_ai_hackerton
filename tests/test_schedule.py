from dataclasses import replace
from datetime import datetime, timedelta, time
from unittest.mock import Mock

import pytest

from config import ScheduleSettings
from models.access import AccessPoint, AccessRoute, AccessStep
from models.place import PlaceCandidate
from models.transport import KST
from models.trip_request import Preference
from providers.http_client import ProviderError
from services.schedule_service import ScheduleService, ScheduleChoice, validate_schedule
from test_places import trip, selected, anchor


def candidates(meal=False, count=4):
    return [PlaceCandidate(place_id=str(i), place_name=f"검증 장소{i}", latitude=36.12+i/10000,
            longitude=128.33, category="음식점 > 한식" if meal else "여행 > 관광명소",
            preference_score=80-i, matched_preferences=(Preference.FOOD if meal else Preference.SIGHTSEEING,),
            role="MAIN_DESTINATION") for i in range(count)]


def scheduler(anchor, **kwargs):
    transit = Mock()
    transit.fastest_route.return_value = AccessRoute(duration_seconds=600, distance_meters=1000, transfers=0,
        steps=(AccessStep(mode="WALKING", duration_seconds=600, distance_meters=1000),))
    return ScheduleService(transit, Mock(return_value=anchor), **kwargs), transit


def test_timeline_start_end_first_and_interplace_routes(trip, selected, anchor):
    service, transit = scheduler(anchor)
    values = candidates()
    result = service.generate(trip, selected, values)
    schedule = result.schedule
    assert schedule.validation_status == "VALIDATED"
    assert schedule.trip_start_datetime == selected.arrival_time
    assert schedule.items[0].item_type == "TRAVEL"
    assert schedule.items[0].start_datetime == selected.arrival_time
    assert schedule.items[0].origin == anchor
    assert schedule.items[-1].end_datetime <= datetime.combine(trip.end_date, trip.end_time, KST)
    visits = [i for i in schedule.items if i.item_type != "TRAVEL"]
    assert len(visits) == 4 and len({i.place_id for i in visits}) == 4
    assert schedule.total_travel_minutes == 40
    assert schedule.total_activity_minutes == 240
    assert all(a.end_datetime <= b.start_datetime for a, b in zip(schedule.items, schedule.items[1:]))
    assert all(i.duration_minutes > 0 for i in schedule.items)


def test_meals_limited_and_unknown_conditions(trip, selected, anchor):
    service, _ = scheduler(anchor)
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=13, minute=25)})
    result = service.generate(trip, selected, candidates(meal=True, count=8))
    meals = [i for i in result.schedule.items if i.item_type == "MEAL"]
    assert len(meals) == 2
    assert len({i.meal_slot for i in meals}) == 2
    assert meals[1].start_datetime.hour >= 17
    assert all(i.open_status == "UNKNOWN" for i in meals)
    assert all("알레르기 정보 확인 필요" in i.checks for i in meals)
    assert all("반려동물 동반 가능 여부 확인 필요" in i.checks for i in meals)
    assert all("아이 동반 이용 조건 확인 필요" in i.checks for i in meals)


@pytest.mark.parametrize("error", ["timeout", "http_429", "http_503", "invalid_llm_output"])
def test_ai_failure_fallback(trip, selected, anchor, error):
    groq = Mock()
    groq.generate_structured.side_effect = ProviderError(error)
    service, _ = scheduler(anchor, groq=groq)
    result = service.generate(trip, selected, candidates())
    assert result.schedule.validation_status == "VALIDATED"
    assert result.ai_status == "기본 일정 생성"
    assert groq.generate_structured.call_count == 1


def test_ai_unknown_id_discarded(trip, selected, anchor):
    groq = Mock()
    groq.generate_structured.return_value = ScheduleChoice(place_ids=["invented"])
    service, _ = scheduler(anchor, groq=groq)
    result = service.generate(trip, selected, candidates())
    assert result.ai_status == "기본 일정 생성"
    assert all(i.place_id != "invented" for i in result.schedule.items)


def test_ai_valid_assistance(trip, selected, anchor):
    groq = Mock()
    groq.generate_structured.return_value = ScheduleChoice(place_ids=["3"])
    service, _ = scheduler(anchor, groq=groq)
    result = service.generate(trip, selected, candidates())
    assert result.ai_status == "AI 보조"
    assert result.schedule.items[0].place_id == "3"


def test_route_unknown_and_alternative(trip, selected, anchor):
    service, transit = scheduler(anchor)
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a,b: (_ for _ in ()).throw(ProviderError("timeout")) if b.id == "0" else route
    result = service.generate(trip, selected, candidates())
    assert result.schedule
    assert all(i.place_id != "0" for i in result.schedule.items)
    transit.fastest_route.side_effect = ProviderError("ACCESS_TIME_UNKNOWN")
    result = service.generate(trip, selected, candidates())
    assert result.schedule is None


def test_short_time_does_not_overfill(trip, selected, anchor):
    service, _ = scheduler(anchor)
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=19,minute=10)})
    assert service.generate(trip, selected, candidates(meal=True)).schedule is None
    rest = candidates()[0].model_copy(update={"category":"카페", "matched_preferences":(Preference.REST,)})
    trip = trip.model_copy(update={"preferences":(Preference.REST,)})
    result = service.generate(trip, selected, [rest])
    assert len(result.schedule.items) == 2
    assert result.schedule.items[-1].end_datetime.hour == 19


def test_preferred_place_and_safe_omission(trip, selected, anchor):
    service, transit = scheduler(anchor)
    result = service.generate(trip, selected, candidates(), "3")
    assert result.schedule.items[0].place_id == "3"
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a,b: route.model_copy(update={"duration_seconds":50000}) if b.id=="3" else route
    result = service.generate(trip, selected, candidates(), "3")
    assert all(i.place_id != "3" for i in result.schedule.items)
    assert any("선택한 장소" in text and "제외" in text for text in result.notices)


def test_missing_inputs_and_budget(trip, selected, anchor):
    service, transit = scheduler(anchor, config=ScheduleSettings(max_route_calls=2))
    assert service.generate(trip, None, candidates()).schedule is None
    assert service.generate(trip, selected, []).schedule is None
    late=selected.model_copy(update={"arrival_time":datetime.combine(trip.end_date,trip.end_time,KST)})
    assert service.generate(trip, late, candidates()).schedule is None
    service.generate(trip, selected, candidates())
    assert transit.fastest_route.call_count <= 2


@pytest.mark.parametrize("tamper", ["overlap", "duration", "teleport", "duplicate", "unknown", "deadline"])
def test_validator_rejects_corruption(trip, selected, anchor, tamper):
    service, transit=scheduler(anchor)
    values=candidates()
    schedule=service.generate(trip, selected, values).schedule
    routes={(call.args[0].id, call.args[1].id):transit.fastest_route.return_value for call in transit.fastest_route.call_args_list}
    items=list(schedule.items)
    if tamper=="overlap":
        items[1]=items[1].model_copy(update={"start_datetime":items[0].start_datetime})
    elif tamper=="duration":
        items[0]=items[0].model_copy(update={"end_datetime":items[0].end_datetime+timedelta(seconds=1)})
    elif tamper=="teleport":
        items.pop(0)
    elif tamper=="duplicate":
        items[3]=items[3].model_copy(update={"place_id":items[1].place_id,"place_name":items[1].place_name})
    elif tamper=="unknown":
        items[1]=items[1].model_copy(update={"place_id":"invented"})
    else:
        items[-1]=items[-1].model_copy(update={"end_datetime":schedule.trip_end_datetime+timedelta(minutes=1)})
    broken=schedule.model_copy(update={"items":tuple(items)})
    assert not validate_schedule(broken,trip,selected,{c.place_id:c for c in values},routes,anchor,service.config)


def test_validation_retry_is_bounded(monkeypatch, trip, selected, anchor):
    validator=Mock(return_value=False)
    monkeypatch.setattr("services.schedule_service.validate_schedule",validator)
    service, transit=scheduler(anchor)
    assert service.generate(trip,selected,candidates()).schedule is None
    assert validator.call_count==service.config.max_schedule_attempts
    assert transit.fastest_route.call_count<=service.config.max_route_calls


def test_main_duplicate_retains_role_nearby_radius(trip, selected, anchor):
    service,_=scheduler(anchor)
    main=candidates()[0].model_copy(update={"latitude":36.15})
    nearby=main.model_copy(update={"role":"NEARBY_PLACE"})
    assert service.generate(trip,selected,[main,nearby]).schedule
    assert service.generate(trip,selected,[nearby]).schedule is None


def test_mixed_preferences_include_evening_without_inventing_places(trip, selected, anchor):
    service, _ = scheduler(anchor)
    values = candidates(count=2) + [candidates(meal=True)[0].model_copy(update={"place_id":"meal"})]
    values.append(candidates()[0].model_copy(update={"place_id":"night", "category":"관광 > 야경",
                                                    "matched_preferences":(Preference.NIGHT_VIEW,)}))
    trip = trip.model_copy(update={"preferences":(Preference.SIGHTSEEING,Preference.FOOD,Preference.NIGHT_VIEW)})
    schedule = service.generate(trip, selected, values).schedule
    visits = [i for i in schedule.items if i.item_type != "TRAVEL"]
    assert {i.preference for i in visits} == {p.value for p in trip.preferences}
    assert next(i for i in visits if i.place_id == "night").start_datetime.hour >= 18


def test_multiday_meal_slots_and_no_zero_arrival_item(trip, selected, anchor):
    service, _ = scheduler(anchor)
    trip = trip.model_copy(update={"end_date":trip.end_date+timedelta(days=1),"preferences":(Preference.FOOD,)})
    schedule = service.generate(trip, selected, candidates(meal=True, count=6)).schedule
    meals = [i for i in schedule.items if i.item_type == "MEAL"]
    assert len(meals) == 4
    assert len({i.meal_slot for i in meals}) == 4
    assert all(i.duration_minutes > 0 for i in schedule.items)


def test_config_and_model_validation():
    from config import load_settings
    from models.schedule import ScheduleItem
    from pydantic import ValidationError
    settings = load_settings(environ={"SCHEDULE_MEAL_MINUTES":"45", "MAX_SCHEDULE_ATTEMPTS":"2"})
    assert settings.schedule.meal_minutes == 45
    assert load_settings(environ={"SCHEDULE_MEAL_MINUTES":"0"}).schedule.meal_minutes == 60
    with pytest.raises(ValueError):
        ScheduleSettings(max_schedule_attempts=100)
    with pytest.raises(ValidationError):
        ScheduleItem(item_type="PLACE",place_id="x",place_name="x",start_datetime=datetime(2026,1,1),end_datetime=datetime(2026,1,1))


def test_arrival_failure_and_long_trip_return_safe_status(trip, selected, anchor):
    service, _ = scheduler(anchor)
    service.resolve_point.side_effect = ProviderError("timeout")
    assert service.generate(trip,selected,candidates()).schedule is None
    service.resolve_point = Mock(return_value=anchor)
    trip = trip.model_copy(update={"end_date":trip.end_date+timedelta(days=100)})
    assert service.generate(trip,selected,candidates()).status == "기간 확인 필요"
