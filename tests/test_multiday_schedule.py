"""Phase 4.6 multi-day / accommodation schedule and lodging UX cases."""
from datetime import datetime, timedelta, time
from unittest.mock import Mock

import pytest

from models.access import AccessPoint
from models.place import PlaceCandidate
from models.schedule import ReturnStatus
from models.transport import KST, TransportType
from models.trip_request import Preference
from providers.http_client import ProviderError
from services.access_service import AccessService
from services.multiday_schedule_service import (
    is_multiday, resolve_accommodation, trip_dates, accommodation_error_message)
from services.return_schedule_service import ReturnScheduleService
from test_places import trip, selected, anchor
from test_schedule import scheduler, candidates


def lodging():
    return AccessPoint(id="stay", name="구미 테스트 숙소", address="경북 구미시 숙소로 1",
                       x=128.34, y=36.13, source="test")


def multiday_trip(trip):
    return trip.model_copy(update={
        "has_accommodation": True,
        "end_date": trip.start_date + timedelta(days=2),
        "end_time": time(20),
        "preferences": (Preference.FOOD, Preference.SIGHTSEEING),
    })


def pool(count=12):
    rows = []
    for i in range(count):
        pref = Preference.FOOD if i % 3 == 0 else Preference.SIGHTSEEING
        rows.append(PlaceCandidate(
            place_id=str(i), place_name=f"검증 장소{i}",
            latitude=36.12 + i / 10000, longitude=128.33,
            category="음식점 > 한식" if pref == Preference.FOOD else "여행 > 관광명소",
            preference_score=90 - i, matched_preferences=(pref,),
            role="MAIN_DESTINATION"))
    return rows


def multiday_service(trip, selected, anchor, *, return_on_end_date=True, fail_resolve=False,
                     address_fallback=False):
    activity, transit = scheduler(anchor)
    home = AccessPoint(id="home", name="검증 출발 건물", address="서울 금천구 검증로 1", x=126.9, y=37.4)
    hub = AccessPoint(id="return-hub", name="구미역", x=128.33, y=36.12)
    arrival_hub = AccessPoint(id="seoul", name="영등포역", x=126.97, y=37.55)
    stay = lodging()
    access = AccessService(Mock(), transit)

    def resolve_origin(query, address=None):
        if fail_resolve and not address_fallback:
            raise ProviderError("INVALID_ORIGIN")
        if address_fallback and address is None and "도로" not in query:
            raise ProviderError("NEED_ADDRESS")
        if address_fallback and address is not None:
            return stay
        if "숙소" in query or "호텔" in query or "도로" in query:
            return stay
        return home

    def resolve_point(name):
        if name == "구미역":
            return hub
        if name == stay.id:
            return stay
        if name in {"영등포역", "서울역"}:
            return arrival_hub
        raise ProviderError("ACCESS_TIME_UNKNOWN")

    access.resolve_origin = Mock(side_effect=resolve_origin)
    access.resolve_point = Mock(side_effect=resolve_point)
    access.places = Mock()
    access.places.search_places = Mock(return_value=[])
    end = trip.end_date
    back = selected.model_copy(update={
        "departure_place": "구미", "arrival_place": "영등포",
        "departure_time": datetime.combine(end, time(16, 0), KST),
        "arrival_time": datetime.combine(end, time(18, 20), KST),
    })
    transport = Mock()
    transport.search_return_candidates.return_value = [back] if return_on_end_date else []
    return ReturnScheduleService(activity, transport, access), transport, home, stay, back, hub


def test_is_multiday_by_date_range_only(trip):
    """Multi-day is date-driven; lodging flag is forced True on TripRequest construction."""
    assert not is_multiday(trip)
    # Same calendar day even with has_accommodation=True is still same-day
    assert not is_multiday(trip.model_copy(update={"has_accommodation": True}))
    # Date span alone makes multi-day; constructor forces has_accommodation=True
    from models.trip_request import TripRequest
    spanned = TripRequest(
        departure=trip.departure, destination=trip.destination,
        start_date=trip.start_date, end_date=trip.start_date + timedelta(days=1),
        departure_time=trip.departure_time, end_time=time(20),
        has_accommodation=False, allergies=(), has_pet=False, has_child=False,
        activity_radius=trip.activity_radius,
        preferences=(Preference.FOOD, Preference.SIGHTSEEING),
    )
    assert spanned.has_accommodation is True
    assert is_multiday(spanned)
    assert is_multiday(multiday_trip(trip))


def test_multiday_builds_three_days_ending_at_lodging(trip, selected, anchor):
    request = multiday_trip(trip)
    service, transport, home, stay, back, hub = multiday_service(request, selected, anchor)
    result = service.generate(request, selected, pool(), "0", "구미 테스트 숙소")
    assert result.schedule and result.schedule.validation_status == "VALIDATED"
    schedule = result.schedule
    assert len(schedule.days) == 3
    assert schedule.accommodation_status == "CONFIRMED"
    assert schedule.days[0].end_location.id == stay.id
    assert schedule.days[1].start_location.id == stay.id
    assert schedule.days[1].end_location.id == stay.id
    assert schedule.days[2].start_location.id == stay.id
    assert schedule.return_status == ReturnStatus.RETURN_AVAILABLE
    reverse, _ = transport.search_return_candidates.call_args.args
    assert reverse.start_date == request.end_date


def test_accommodation_address_fallback_confirms_lodging(trip, selected, anchor):
    request = multiday_trip(trip)
    service, *_rest, stay, _back, _hub = multiday_service(
        request, selected, anchor, address_fallback=True)
    result = service.generate(request, selected, pool(), None, "경북 구미시 숙소로 1")
    # NEED_ADDRESS on name path then address-shaped resolve_origin(query, query).
    assert result.schedule and result.schedule.accommodation_status == "CONFIRMED"
    assert result.schedule.accommodation.id == stay.id


def test_accommodation_resolve_failure_message(trip, selected, anchor):
    request = multiday_trip(trip)
    service, *_ = multiday_service(request, selected, anchor, fail_resolve=True)
    result = service.generate(request, selected, pool(), None, "알수없는숙소XYZ")
    assert result.schedule is None
    assert result.notices == [accommodation_error_message()]
    assert "도로명 주소" in result.notices[0]


def test_missing_accommodation_returns_empty_notices_for_ui(trip, selected, anchor):
    request = multiday_trip(trip)
    service, *_ = multiday_service(request, selected, anchor)
    result = service.generate(request, selected, pool(), None, "")
    assert result.schedule is None
    assert result.status == "숙소 입력 필요"
    assert result.notices == []


def test_provisional_undecided_uses_arrival_hub(trip, selected, anchor):
    request = multiday_trip(trip)
    service, transport, _home, _stay, _back, hub = multiday_service(request, selected, anchor)
    result = service.generate(request, selected, pool(), "0", accommodation_undecided=True)
    assert result.schedule is not None
    assert result.schedule.accommodation_status == "PROVISIONAL"
    assert result.schedule.accommodation.id == hub.id
    assert result.schedule.accommodation.name != request.destination
    assert result.schedule.accommodation.name == hub.name
    assert result.schedule.days[0].end_location.id == hub.id
    assert result.schedule.days[1].start_location.id == hub.id
    assert result.schedule.days[1].end_location.id == hub.id
    assert result.schedule.days[2].start_location.id == hub.id
    assert any("임시 기준점" in n for n in result.notices)
    reverse, _ = transport.search_return_candidates.call_args.args
    assert reverse.start_date == request.end_date


def test_provisional_bus_hub_not_destination_string(trip, selected, anchor):
    """CASE: EXPRESS_BUS arrival short-name '구미' resolves to terminal POI, not destination string."""
    request = multiday_trip(trip)
    bus = selected.model_copy(update={
        "transport_type": TransportType.EXPRESS_BUS,
        "departure_place": "서울경부", "arrival_place": "구미",
    })
    terminal = AccessPoint(id="gumi-terminal", name="구미종합터미널", x=128.38, y=36.12)
    seoul = AccessPoint(id="seoul-bus", name="서울경부", x=127.0, y=37.48)
    service, *_rest, stay, _back, _hub = multiday_service(request, bus, anchor)
    access = service.access

    def resolve_point(name):
        if name in {"구미버스터미널", "구미종합터미널"}:
            return terminal
        if name in {"영등포버스터미널", "서울경부버스터미널", "영등포역", "서울역"}:
            return seoul
        if name == stay.id:
            return stay
        raise ProviderError("ACCESS_TIME_UNKNOWN")

    access.resolve_point = Mock(side_effect=resolve_point)
    result = service.generate(request, bus, pool(), "0", accommodation_undecided=True)
    assert result.schedule is not None
    assert result.status != "일정 검증 실패"
    assert result.schedule.accommodation_status == "PROVISIONAL"
    assert result.schedule.accommodation.id == terminal.id
    assert result.schedule.accommodation.name == "구미종합터미널"
    assert result.schedule.accommodation.name != "구미"
    assert result.schedule.accommodation.name != request.destination
    assert access.resolve_point.call_args_list[0].args[0] == "구미버스터미널"
    for day in result.schedule.days:
        if day.role == "FIRST":
            assert day.end_location.id == terminal.id
        elif day.role == "MIDDLE":
            assert day.start_location.id == terminal.id
            assert day.end_location.id == terminal.id
        else:
            assert day.start_location.id == terminal.id


def test_same_access_point_stable_id_and_coords():
    from services.multiday_schedule_service import same_access_point
    left = AccessPoint(id="hub", name="구미종합터미널", x=128.38, y=36.12)
    right = AccessPoint(id="hub", name="구미종합터미널", x=128.38, y=36.12, address="경북 구미시")
    renamed = AccessPoint(id="other", name="구미종합터미널", x=128.3801, y=36.1201)
    assert same_access_point(left, right)
    assert left is not right
    assert same_access_point(left, renamed)
    assert not same_access_point(left, AccessPoint(id="x", name="구미역", x=128.33, y=36.12))


def test_validate_multiday_accepts_provisional_separate_hub_objects(trip):
    from services.multiday_schedule_service import validate_multiday, validate_multiday_reason
    from models.schedule import TripDaySchedule, TripSchedule
    request = multiday_trip(trip)
    hub = AccessPoint(id="gumi-terminal", name="구미종합터미널", x=128.38, y=36.12)
    hub_copy = AccessPoint(id="gumi-terminal", name="구미종합터미널", x=128.38, y=36.12, address="경북")
    days = (
        TripDaySchedule(day_index=1, date=request.start_date, role="FIRST", start_location=hub,
                        end_location=hub_copy, activity_start=datetime.combine(request.start_date, time(13), KST),
                        activity_end=datetime.combine(request.start_date, time(21), KST), items=()),
        TripDaySchedule(day_index=2, date=request.start_date + timedelta(days=1), role="MIDDLE",
                        start_location=hub_copy, end_location=hub,
                        activity_start=datetime.combine(request.start_date + timedelta(days=1), time(9), KST),
                        activity_end=datetime.combine(request.start_date + timedelta(days=1), time(21), KST),
                        items=()),
        TripDaySchedule(day_index=3, date=request.end_date, role="FINAL", start_location=hub_copy,
                        end_location=hub, activity_start=datetime.combine(request.end_date, time(9), KST),
                        activity_end=datetime.combine(request.end_date, time(9), KST), items=()),
    )
    schedule = TripSchedule(
        trip_start_datetime=days[0].activity_start,
        trip_end_datetime=datetime.combine(request.end_date, request.end_time, KST),
        arrival_point=hub, items=(), days=days, accommodation=hub,
        accommodation_status="PROVISIONAL",
        accommodation_nights=(hub, hub),
        return_status=ReturnStatus.RETURN_NONE,
        validation_status="VALIDATED")
    assert validate_multiday_reason(schedule, request, hub_copy) == ""
    assert validate_multiday(schedule, request, hub_copy)


def test_gap_fill_skips_prior_day_place_ids(trip, selected, anchor):
    """Supporting gap fill must not reuse exclude_ids (cross-day provisional hub collision)."""
    from dataclasses import replace
    from config import ScheduleSettings
    from models.schedule import ScheduleItem
    from test_schedule import scheduler

    prior = PlaceCandidate(
        place_id="reuse-me", place_name="재사용 카페", latitude=36.12, longitude=128.33,
        category="음식점 > 카페", preference_score=80, matched_preferences=(Preference.REST,),
        role="NEARBY_PLACE", address="경북 구미시")
    fresh = PlaceCandidate(
        place_id="fresh", place_name="신규 카페", latitude=36.121, longitude=128.331,
        category="음식점 > 카페", preference_score=70, matched_preferences=(Preference.REST,),
        role="NEARBY_PLACE", address="경북 구미시")
    hub = AccessPoint(id="return-hub", name="구미역", x=128.33, y=36.12)
    service, transit = scheduler(anchor, supporting_search=Mock(return_value=[prior, fresh]),
                                 config=replace(ScheduleSettings(), min_supporting_activity_gap_minutes=60,
                                                accommodation_return_buffer_minutes=15))
    # Force a large trailing gap by building a short day then filling with exclude.
    start = datetime.combine(trip.start_date, time(12), KST)
    deadline = datetime.combine(trip.start_date, time(20), KST)
    base = [
        ScheduleItem(item_type="PLACE", place_id="0", place_name="검증 장소0",
                     start_datetime=start, end_datetime=start + timedelta(hours=1),
                     reason="테스트"),
    ]
    # Seed routes so append/end can succeed if needed — exercise _fill_gaps directly.
    items, supporting = service._fill_gaps(
        trip.model_copy(update={"end_time": time(20)}),
        selected.model_copy(update={"arrival_time": start}),
        hub, base, {}, {}, {c.place_id: c for c in pool(3)},
        exclude_ids=frozenset({"reuse-me"}))
    used = {i.place_id for i in items if i.item_type != "TRAVEL"}
    assert "reuse-me" not in used
    assert "reuse-me" not in supporting


def test_arrival_hub_query_never_bare_destination(selected):
    from services.multiday_schedule_service import arrival_hub_query
    assert arrival_hub_query(selected) == "구미역"
    bus = selected.model_copy(update={
        "transport_type": TransportType.EXPRESS_BUS,
        "arrival_place": "구미",
    })
    assert arrival_hub_query(bus) == "구미버스터미널"
    assert arrival_hub_query(bus) != "구미"


def test_confirmed_after_provisional_rebuilds(trip, selected, anchor):
    request = multiday_trip(trip)
    service, *_rest, stay, _back, hub = multiday_service(request, selected, anchor)
    first = service.generate(request, selected, pool(), "0", accommodation_undecided=True)
    second = service.generate(request, selected, pool(), "0", "구미 테스트 숙소",
                              accommodation_point=stay)
    assert first.schedule.accommodation_status == "PROVISIONAL"
    assert first.schedule.accommodation.id == hub.id
    assert second.schedule.accommodation_status == "CONFIRMED"
    assert second.schedule.accommodation.id == stay.id
    assert first.schedule.accommodation.id != second.schedule.accommodation.id


def test_anchor_change_stales_all_days(trip, selected, anchor):
    request = multiday_trip(trip)
    service, *_ = multiday_service(request, selected, anchor)
    values = pool()
    first = service.generate(request, selected, values, "0", "구미 테스트 숙소")
    second = service.generate(request, selected, values, "1", "구미 테스트 숙소")
    assert first.schedule and second.schedule
    assert second.schedule.user_selected_place_id == "1"
    assert [tuple(i.place_id for i in d.items) for d in first.schedule.days] != [
        tuple(i.place_id for i in d.items) for d in second.schedule.days]


def test_single_day_regression_ignores_accommodation_query(trip, selected, anchor):
    service, *_ = multiday_service(trip, selected, anchor)
    result = service.generate(trip, selected, candidates(), "0", "구미 테스트 숙소")
    assert result.schedule and not result.schedule.days
    assert result.schedule.return_status == ReturnStatus.RETURN_AVAILABLE


def test_resolve_accommodation_reuses_origin_resolver():
    access = Mock()
    stay = lodging()
    access.resolve_origin.return_value = stay
    assert resolve_accommodation(access, "구미 테스트 숙소") == stay
    access.resolve_origin.assert_called_once_with("구미 테스트 숙소")


def test_ui_lodging_choice_and_no_duplicate_warning(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from models.place import PlaceResult
    from models.schedule import ScheduleResult, TripDaySchedule, TripSchedule
    from test_local_origin import setup

    service, trip_req, *_ = setup()
    request = multiday_trip(trip_req)
    transport_result = service.search(trip_req)
    stay = lodging()
    arrival = AccessPoint(id="station", name="구미역", x=128.33, y=36.12)
    day = TripDaySchedule(
        day_index=1, date=request.start_date, role="FIRST", start_location=arrival,
        end_location=stay, activity_start=datetime.combine(request.start_date, time(13), KST),
        activity_end=datetime.combine(request.start_date, time(21), KST), items=())
    days = (
        day,
        day.model_copy(update={
            "day_index": 2, "date": request.start_date + timedelta(days=1), "role": "MIDDLE",
            "start_location": stay, "activity_start": datetime.combine(
                request.start_date + timedelta(days=1), time(9), KST),
            "activity_end": datetime.combine(request.start_date + timedelta(days=1), time(21), KST),
        }),
        day.model_copy(update={
            "day_index": 3, "date": request.end_date, "role": "FINAL",
            "start_location": stay, "end_location": stay,
            "activity_start": datetime.combine(request.end_date, time(9), KST),
            "activity_end": datetime.combine(request.end_date, time(9), KST),
        }),
    )
    schedule = TripSchedule(
        trip_start_datetime=days[0].activity_start, trip_end_datetime=datetime.combine(
            request.end_date, request.end_time, KST),
        arrival_point=arrival, items=(), days=days, accommodation=stay,
        accommodation_status="CONFIRMED",
        return_status=ReturnStatus.RETURN_NONE, validation_status="VALIDATED")
    values = pool(3)
    generate = Mock(return_value=ScheduleResult("생성 완료", schedule))
    resolve = Mock(return_value=(stay, None))
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=transport_result))
    monkeypatch.setattr("services.place_service.search_places_for_trip",
                        Mock(return_value=PlaceResult(candidates=values, destination_scope="구미")))
    monkeypatch.setattr("services.schedule_service.generate_trip_schedule", generate)
    monkeypatch.setattr("services.schedule_service.resolve_trip_accommodation", resolve)
    from models.accommodation import AccommodationRecommendResult
    monkeypatch.setattr(
        "services.accommodation_recommend_service.search_accommodation_candidates",
        Mock(return_value=AccommodationRecommendResult(status="empty", notices=("없음",))))
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(request.departure)
    app.text_input[1].set_value(request.destination)
    app.date_input[1].set_value(request.end_date)
    app.multiselect[0].set_value(["맛집", "관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    assert "숙소" in [s.value for s in app.subheader]
    # Multi-day date range shows lodging panel (known vs undecided) without a separate overnight checkbox.
    app.radio(key="accommodation_choice_radio").set_value("숙소를 정했어요").run()
    lodging_inputs = [i for i in app.text_input if "숙소명 또는 주소" in (i.label or "")]
    assert lodging_inputs
    lodging_inputs[0].set_value("구미 테스트 숙소").run()
    app.button(key="confirm_accommodation").click().run()
    assert app.session_state.accommodation_point.id == stay.id
    warning = "숙박 여행 일정을 생성하려면 숙소명 또는 주소를 확인해주세요."
    visible = "\n".join(item.value for group in (app.info, app.warning, app.caption, app.markdown)
                        for item in group)
    assert visible.count(warning) == 0
    app.button(key="search_nearby_places").click().run()
    app.button(key="anchor_schedule_0").click().run()
    assert generate.call_count == 1
    assert generate.call_args.kwargs.get("accommodation_point") == stay
    assert generate.call_args.kwargs.get("accommodation_undecided") is False
    assert not app.exception


def test_ui_same_day_hides_lodging_panel(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from test_local_origin import setup
    service, trip_req, *_ = setup()
    monkeypatch.setattr("services.transport_service.search_transport",
                        Mock(return_value=service.search(trip_req)))
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip_req.departure)
    app.text_input[1].set_value(trip_req.destination)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    assert "숙소" not in [s.value for s in app.subheader]
    assert "accommodation_choice_radio" not in [getattr(r, "key", None) for r in app.radio]
    assert not app.exception


def test_ui_provisional_then_confirm_stales(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from models.place import PlaceResult
    from models.schedule import ScheduleResult, TripDaySchedule, TripSchedule
    from test_local_origin import setup

    service, trip_req, *_ = setup()
    request = multiday_trip(trip_req)
    transport_result = service.search(trip_req)
    stay = lodging()
    hub = AccessPoint(id="station", name="구미역", x=128.33, y=36.12)
    base_day = TripDaySchedule(
        day_index=1, date=request.start_date, role="FIRST", start_location=hub,
        end_location=hub, activity_start=datetime.combine(request.start_date, time(13), KST),
        activity_end=datetime.combine(request.start_date, time(21), KST), items=())
    days = (
        base_day,
        base_day.model_copy(update={
            "day_index": 2, "date": request.start_date + timedelta(days=1), "role": "MIDDLE",
            "activity_start": datetime.combine(request.start_date + timedelta(days=1), time(9), KST),
            "activity_end": datetime.combine(request.start_date + timedelta(days=1), time(21), KST),
        }),
        base_day.model_copy(update={
            "day_index": 3, "date": request.end_date, "role": "FINAL",
            "activity_start": datetime.combine(request.end_date, time(9), KST),
            "activity_end": datetime.combine(request.end_date, time(9), KST),
        }),
    )
    provisional = TripSchedule(
        trip_start_datetime=days[0].activity_start,
        trip_end_datetime=datetime.combine(request.end_date, request.end_time, KST),
        arrival_point=hub, items=(), days=days, accommodation=hub,
        accommodation_status="PROVISIONAL", return_status=ReturnStatus.RETURN_NONE,
        validation_status="VALIDATED")
    confirmed = provisional.model_copy(update={
        "accommodation": stay, "accommodation_status": "CONFIRMED",
        "days": tuple(d.model_copy(update={
            "start_location": hub if d.role == "FIRST" else stay,
            "end_location": stay if d.role != "FINAL" else stay,
        }) for d in days),
    })
    generate = Mock(side_effect=[
        ScheduleResult("생성 완료", provisional),
        ScheduleResult("생성 완료", confirmed),
    ])
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=transport_result))
    monkeypatch.setattr("services.place_service.search_places_for_trip",
                        Mock(return_value=PlaceResult(candidates=pool(3), destination_scope="구미")))
    monkeypatch.setattr("services.schedule_service.generate_trip_schedule", generate)
    monkeypatch.setattr("services.schedule_service.resolve_trip_accommodation",
                        Mock(return_value=(stay, None)))
    from models.accommodation import AccommodationRecommendResult
    monkeypatch.setattr(
        "services.accommodation_recommend_service.search_accommodation_candidates",
        Mock(return_value=AccommodationRecommendResult(status="empty", notices=("없음",))))
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(request.departure)
    app.text_input[1].set_value(request.destination)
    app.date_input[1].set_value(request.end_date)
    app.multiselect[0].set_value(["맛집", "관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.radio(key="accommodation_choice_radio").set_value("아직 숙소를 정하지 않았어요").run()
    app.button(key="search_nearby_places").click().run()
    app.button(key="anchor_schedule_0").click().run()
    assert generate.call_args.kwargs.get("accommodation_undecided") is True
    assert app.session_state.trip_schedule.accommodation_status == "PROVISIONAL"
    texts = []
    for group in (app.markdown, app.text, app.caption, app.info):
        for item in group:
            texts.append(getattr(item, "value", "") or str(item))
    joined = "\n".join(texts)
    assert "을(를)" not in joined
    assert any("임시 기준점:" in t for t in texts)
    app.radio(key="accommodation_choice_radio").set_value("숙소를 정했어요").run()
    assert "trip_schedule" not in app.session_state
    lodging_inputs = [i for i in app.text_input if "숙소명 또는 주소" in (i.label or "")]
    lodging_inputs[0].set_value("구미 테스트 숙소").run()
    app.button(key="confirm_accommodation").click().run()
    assert generate.call_count == 2
    assert generate.call_args.kwargs.get("accommodation_undecided") is False
    assert app.session_state.trip_schedule.accommodation_status == "CONFIRMED"
    assert not app.exception


def test_ui_recommend_select_confirms_and_stales(monkeypatch):
    """CASE 5/9/10 — recommend select → CONFIRMED; change lodging → schedule stale."""
    from streamlit.testing.v1 import AppTest
    from models.accommodation import AccommodationCandidate, AccommodationRecommendResult
    from models.place import PlaceResult
    from models.schedule import ScheduleResult, TripDaySchedule, TripSchedule
    from test_local_origin import setup

    service, trip_req, *_ = setup()
    request = multiday_trip(trip_req)
    transport_result = service.search(trip_req)
    stay_a = lodging()
    stay_b = AccessPoint(id="stay-b", name="구미 두번째 숙소", address="경북 구미시 숙소로 2",
                         x=128.35, y=36.14, source="test")
    hub = AccessPoint(id="station", name="구미역", x=128.33, y=36.12)
    candidate_a = AccommodationCandidate(
        place_id=stay_a.id, place_name=stay_a.name, address=stay_a.address,
        category="숙박 > 호텔", latitude=stay_a.y, longitude=stay_a.x,
        reason="이동 거리가 짧은 후보입니다.")
    candidate_b = AccommodationCandidate(
        place_id=stay_b.id, place_name=stay_b.name, address=stay_b.address,
        category="숙박 > 호텔", latitude=stay_b.y, longitude=stay_b.x,
        reason="여행 동선상 이동 부담이 적은 후보입니다.")

    def day_for(stay, role, index, day_date):
        start = hub if role == "FIRST" else stay
        end = stay if role != "FINAL" else stay
        return TripDaySchedule(
            day_index=index, date=day_date, role=role, start_location=start,
            end_location=end,
            activity_start=datetime.combine(day_date, time(13 if role == "FIRST" else 9), KST),
            activity_end=datetime.combine(day_date, time(21 if role != "FINAL" else 9), KST),
            items=())

    def schedule_for(stay, status):
        days = (
            day_for(stay, "FIRST", 1, request.start_date),
            day_for(stay, "MIDDLE", 2, request.start_date + timedelta(days=1)),
            day_for(stay, "FINAL", 3, request.end_date),
        )
        return TripSchedule(
            trip_start_datetime=days[0].activity_start,
            trip_end_datetime=datetime.combine(request.end_date, request.end_time, KST),
            arrival_point=hub, items=(), days=days, accommodation=stay,
            accommodation_status=status, return_status=ReturnStatus.RETURN_NONE,
            validation_status="VALIDATED")

    generate = Mock(side_effect=[
        ScheduleResult("생성 완료", schedule_for(stay_a, "CONFIRMED")),
        ScheduleResult("생성 완료", schedule_for(stay_b, "CONFIRMED")),
    ])
    recommend = Mock(return_value=AccommodationRecommendResult(
        status="ok", candidates=(candidate_a, candidate_b),
        notices=("여행 동선과 위치를 기준으로 찾은 숙소입니다. "
                 "객실 가격과 예약 가능 여부는 숙박 예약 서비스에서 확인해주세요.",)))
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=transport_result))
    monkeypatch.setattr("services.place_service.search_places_for_trip",
                        Mock(return_value=PlaceResult(candidates=pool(3), destination_scope="구미")))
    monkeypatch.setattr("services.schedule_service.generate_trip_schedule", generate)
    monkeypatch.setattr(
        "services.accommodation_recommend_service.search_accommodation_candidates", recommend)
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(request.departure)
    app.text_input[1].set_value(request.destination)
    app.date_input[1].set_value(request.end_date)
    app.multiselect[0].set_value(["맛집", "관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.radio(key="accommodation_choice_radio").set_value("아직 숙소를 정하지 않았어요").run()
    assert recommend.call_count >= 1
    assert "예약 가능한 숙소" not in "\n".join(c.value for c in app.caption)
    app.button(key=f"select_lodging_{stay_a.id}").click().run()
    assert app.session_state.accommodation_point.id == stay_a.id
    assert app.session_state.accommodation_source == "recommended"
    app.button(key="search_nearby_places").click().run()
    app.button(key="anchor_schedule_0").click().run()
    assert generate.call_count == 1
    assert generate.call_args.kwargs.get("accommodation_undecided") is False
    assert generate.call_args.kwargs.get("accommodation_point").id == stay_a.id
    assert app.session_state.trip_schedule.accommodation_status == "CONFIRMED"
    app.button(key="clear_recommended_accommodation").click().run()
    assert "trip_schedule" not in app.session_state
    app.button(key=f"select_lodging_{stay_b.id}").click().run()
    assert generate.call_count == 2
    assert generate.call_args.kwargs.get("accommodation_point").id == stay_b.id
    assert not app.exception


def test_ui_return_only_on_final_day_and_seat_copy(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from models.place import PlaceResult
    from models.schedule import ScheduleResult, TripDaySchedule, TripSchedule, ReturnJourney
    from models.access import AccessLeg
    from test_local_origin import setup

    service, trip_req, *_ = setup()
    request = multiday_trip(trip_req)
    transport_result = service.search(trip_req)
    stay = lodging()
    arrival = AccessPoint(id="station", name="구미역", x=128.33, y=36.12)
    day = TripDaySchedule(
        day_index=1, date=request.start_date, role="FIRST", start_location=arrival,
        end_location=stay, activity_start=datetime.combine(request.start_date, time(13), KST),
        activity_end=datetime.combine(request.start_date, time(21), KST), items=())
    days = (
        day,
        day.model_copy(update={
            "day_index": 2, "date": request.start_date + timedelta(days=1), "role": "MIDDLE",
            "start_location": stay, "activity_start": datetime.combine(
                request.start_date + timedelta(days=1), time(9), KST),
            "activity_end": datetime.combine(request.start_date + timedelta(days=1), time(21), KST),
        }),
        day.model_copy(update={
            "day_index": 3, "date": request.end_date, "role": "FINAL",
            "start_location": stay, "end_location": stay,
            "activity_start": datetime.combine(request.end_date, time(9), KST),
            "activity_end": datetime.combine(request.end_date, time(14, 51), KST),
        }),
    )
    back = transport_result.candidates[0].model_copy(update={
        "departure_place": "구미", "arrival_place": "영등포",
        "departure_time": datetime.combine(request.end_date, time(15, 50), KST),
        "arrival_time": datetime.combine(request.end_date, time(18, 28), KST),
    })
    to_hub = AccessLeg(origin="인동향교", destination="구미역", transport_modes=("WALKING",),
                       duration_minutes=30,
                       departure_time=datetime.combine(request.end_date, time(14, 51), KST),
                       provider="test")
    to_origin = AccessLeg(origin="영등포역", destination="출발지", transport_modes=("SUBWAY",),
                          duration_minutes=25, departure_time=back.arrival_time, provider="test")
    schedule = TripSchedule(
        trip_start_datetime=days[0].activity_start, trip_end_datetime=datetime.combine(
            request.end_date, request.end_time, KST),
        arrival_point=arrival, items=(), days=days, accommodation=stay,
        accommodation_status="PROVISIONAL",
        return_journey=ReturnJourney(to_hub=to_hub, transport=back, boarding_buffer_minutes=15,
                                     to_origin=to_origin),
        return_status=ReturnStatus.RETURN_AVAILABLE,
        final_arrival_datetime=to_origin.arrival_time,
        destination_activity_cutoff=datetime.combine(request.end_date, time(15, 4), KST),
        validation_status="VALIDATED")
    generate = Mock(return_value=ScheduleResult("생성 완료", schedule, notices=[
        "체류시간은 기본 추정값입니다. 영업시간과 실제 배차·지연은 방문 전에 확인해주세요.",
        "체류시간은 기본 추정값입니다. 영업시간과 실제 배차·지연은 방문 전에 확인해주세요.",
    ]))
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=transport_result))
    monkeypatch.setattr("services.place_service.search_places_for_trip",
                        Mock(return_value=PlaceResult(candidates=pool(3), destination_scope="구미")))
    monkeypatch.setattr("services.schedule_service.generate_trip_schedule", generate)
    from models.accommodation import AccommodationRecommendResult
    monkeypatch.setattr(
        "services.accommodation_recommend_service.search_accommodation_candidates",
        Mock(return_value=AccommodationRecommendResult(status="empty", notices=("없음",))))
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(request.departure)
    app.text_input[1].set_value(request.destination)
    app.date_input[1].set_value(request.end_date)
    app.multiselect[0].set_value(["맛집", "관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    assert any("좌석/매진 여부는 예매처에서 확인 필요" in c.value for c in app.caption)
    assert not any("예약되지 않았습니다" in c.value for c in app.caption)
    app.radio(key="accommodation_choice_radio").set_value("아직 숙소를 정하지 않았어요").run()
    app.button(key="search_nearby_places").click().run()
    app.button(key="anchor_schedule_0").click().run()
    dwell = "체류시간은 기본 추정값입니다"
    assert sum(1 for v in app.info if dwell in v.value) == 0
    assert any(
        str(getattr(e, "label", "")).startswith("돌아가는 길") for e in app.expander)
    assert any("활동 종료 권장 한도" in c.value for c in app.caption)
    assert any("15:50" in getattr(m, "value", "") for m in list(app.markdown) + list(app.caption))
    assert not app.exception
