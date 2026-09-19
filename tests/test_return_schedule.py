from datetime import datetime, timedelta, time
from unittest.mock import Mock

import pytest

from models.access import AccessPoint
from models.schedule import ReturnStatus, ScheduleResult
from models.transport import KST, TransportType
from providers.http_client import ProviderError
from services.access_service import AccessService
from services.return_schedule_service import ReturnScheduleService
from services.transport_service import TransportService, rank_candidates, rank_return_candidates, convert_candidate
from test_schedule import scheduler, candidates
from test_places import trip, selected, anchor


def round_trip(trip, selected, anchor):
    activity, transit = scheduler(anchor)
    home = AccessPoint(id="home", name="검증 출발 건물", address="서울 금천구 검증로 1", x=126.9, y=37.4)
    hub = AccessPoint(id="return-hub", name="구미역", x=128.33, y=36.12)
    arrival_hub = AccessPoint(id="seoul", name="영등포역", x=126.97, y=37.55)
    access = AccessService(Mock(), transit)
    access.resolve_origin = Mock(return_value=home)
    access.resolve_point = Mock(side_effect=lambda name: hub if name == "구미역" else arrival_hub)
    back = selected.model_copy(update={"departure_place": "구미", "arrival_place": "영등포",
        "departure_time": selected.arrival_time.replace(hour=17, minute=0),
        "arrival_time": selected.arrival_time.replace(hour=19, minute=20)})
    transport = Mock()
    transport.search_return_candidates.return_value = [back]
    return ReturnScheduleService(activity, transport, access), transport, home, back


def test_return_to_original_origin_with_anchor_and_cutoff(trip, selected, anchor):
    service, transport, home, back = round_trip(trip, selected, anchor)
    values = candidates()
    result = service.generate(trip, selected, values, values[1].place_id)
    assert result.schedule and result.schedule.validation_status == "VALIDATED"
    schedule = result.schedule
    assert schedule.return_status == ReturnStatus.RETURN_AVAILABLE
    journey = schedule.return_journey
    assert journey.to_origin.destination_point == home
    assert schedule.final_arrival_datetime.hour == 19
    assert schedule.final_arrival_datetime <= schedule.trip_end_datetime
    assert journey.to_hub.arrival_time + timedelta(minutes=15) <= back.departure_time
    assert schedule.items[-1].end_datetime <= journey.to_hub.departure_time
    assert schedule.destination_activity_cutoff is not None
    assert {i.place_id for i in schedule.items} == {values[1].place_id}
    reverse, actual_home = transport.search_return_candidates.call_args.args
    assert reverse.departure == trip.destination and actual_home == home
    assert reverse.start_date == trip.end_date


def test_anchor_change_does_not_copy_old_supporting(trip, selected, anchor):
    service, _, _, _ = round_trip(trip, selected, anchor)
    values = candidates()
    first = service.generate(trip, selected, values, values[0].place_id)
    second = service.generate(trip, selected, values, values[1].place_id)
    assert first.schedule and second.schedule
    assert all(i.place_id != values[0].place_id for i in second.schedule.items)


@pytest.mark.parametrize("failure,status", [
    ("no-return", ReturnStatus.RETURN_NONE),
    ("late-home", ReturnStatus.RETURN_INFEASIBLE),
    ("anchor-too-late", ReturnStatus.RETURN_INFEASIBLE),
    ("route-failure", ReturnStatus.RETURN_INFEASIBLE),
])
def test_infeasible_return_keeps_outbound_and_reports_status(trip, selected, anchor, failure, status):
    service, transport, _, back = round_trip(trip, selected, anchor)
    if failure == "no-return":
        transport.search_return_candidates.return_value = []
    if failure == "late-home":
        transport.search_return_candidates.return_value = [
            back.model_copy(update={"arrival_time": back.arrival_time.replace(hour=20)})]
    if failure == "anchor-too-late":
        transport.search_return_candidates.return_value = [
            back.model_copy(update={"departure_time": selected.arrival_time + timedelta(minutes=20)})]
    if failure == "route-failure":
        service.access.transit.fastest_route.side_effect = ProviderError("timeout")
    result = service.generate(trip, selected, candidates(), "0")
    assert result.schedule is None or result.schedule.return_journey is None
    if result.schedule is not None:
        assert result.schedule.return_status == status
    assert any("귀가" in n or "왕복" in n for n in result.notices)


def test_return_provider_timeout_keeps_activity_unknown(trip, selected, anchor):
    service, transport, _, _ = round_trip(trip, selected, anchor)
    transport.search_return_candidates.side_effect = ProviderError("timeout")
    result = service.generate(trip, selected, candidates(), "0")
    assert result.schedule is None or result.schedule.return_status == ReturnStatus.RETURN_UNKNOWN
    assert any("귀가 교통편을 확인하지 못했습니다" in n for n in result.notices)


def test_earlier_return_alternative_is_used(trip, selected, anchor):
    service, transport, _, back = round_trip(trip, selected, anchor)
    late = back.model_copy(update={"departure_time": back.departure_time + timedelta(hours=1),
                                  "arrival_time": back.arrival_time + timedelta(hours=1)})
    transport.search_return_candidates.return_value = [late, back]
    result = service.generate(trip, selected, candidates(), "0")
    assert result.schedule.return_journey.transport == back


def test_return_search_reuses_all_modes(trip, selected, anchor):
    from models.transport import TransportStatus
    service = TransportService(Mock(), Mock(), Mock(), today=lambda: trip.start_date)
    service._search_mode = Mock(side_effect=lambda t, k, p, d, a, r: ([], TransportStatus(k, "정상", "")))
    reverse = trip.model_copy(update={"departure": "구미", "destination": "서울", "start_date": trip.end_date})
    home = AccessPoint(id="home", name="검증 주소", address="서울 금천구 검증로 1", x=126.9, y=37.4)
    service.search_return_candidates(reverse, home)
    assert {c.args[1] for c in service._search_mode.call_args_list} == set(TransportType)
    assert all(c.args[0].departure == "구미" for c in service._search_mode.call_args_list)
    # Arrival context is origin-region LOCAL_ORIGIN target.
    assert all(c.args[4].city == "서울" for c in service._search_mode.call_args_list)


def test_outbound_ranking_ignores_end_time(trip):
    row = convert_candidate({"depplacename": "영등포", "arrplacename": "구미",
                             "depplandtime": "20261001093000", "arrplandtime": "20261001210000",
                             "adultcharge": "10000"}, TransportType.TRAIN)
    assert rank_candidates([row], trip) == [row]
    earliest = datetime.combine(trip.start_date, time(12), KST)
    assert rank_return_candidates([row], trip, earliest_departure=earliest) == []


def test_system_now_not_used_for_outbound_filter(trip):
    # Planned trip times decide eligibility; wall-clock "now" is irrelevant.
    row = convert_candidate({"depplacename": "영등포", "arrplacename": "구미",
                             "depplandtime": "20261001093000", "arrplandtime": "20261001120000"},
                            TransportType.TRAIN)
    assert rank_candidates([row], trip) == [row]


def test_main_card_click_rebuilds_anchor_schedule(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from models.place import PlaceResult
    from test_local_origin import setup
    service, trip, *_ = setup()
    result = service.search(trip)
    values = candidates()
    generate = Mock(return_value=ScheduleResult("귀가 조건 미충족"))
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=result))
    monkeypatch.setattr("services.place_service.search_places_for_trip", Mock(return_value=PlaceResult(candidates=values, destination_scope="구미")))
    monkeypatch.setattr("services.schedule_service.generate_trip_schedule", generate)
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip.departure)
    app.text_input[1].set_value(trip.destination)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.button(key="search_nearby_places").click().run()
    app.session_state.trip_schedule = "stale"
    app.button(key="anchor_schedule_0").click().run()
    assert app.session_state.user_selected_place_id == "0"
    assert app.session_state.main_place_list_expanded is False
    assert generate.call_args.args[4] == "0"
    assert app.session_state.trip_schedule is None
    # List collapsed — reopen to pick another MAIN
    app.button(key="expand_main_place_list").click().run()
    assert app.session_state.main_place_list_expanded is True
    app.session_state.trip_schedule = "stale"
    app.button(key="anchor_schedule_1").click().run()
    assert app.session_state.user_selected_place_id == "1"
    assert app.session_state.main_place_list_expanded is False
    assert generate.call_args.args[4] == "1"
    assert "nearby_result" not in app.session_state
    app.run()
    assert generate.call_count == 2 and not app.exception


def test_compact_outbound_ui_stays_collapsed(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from models.transport import TransportResult, TransportStatus
    from test_local_origin import setup
    service, trip, *_ = setup()
    base = service.search(trip).candidates[0]
    rows = [base.model_copy(update={"train_number": str(i), "departure_time": base.departure_time + timedelta(minutes=i),
                                    "arrival_time": base.arrival_time + timedelta(minutes=i)}) for i in range(5)]
    result = TransportResult(candidates=rows, by_type={TransportType.TRAIN: rows},
                             statuses=[TransportStatus(TransportType.TRAIN, "정상", "")], origin_status="LOCAL_ORIGIN")
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=result))
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip.departure)
    app.text_input[1].set_value(trip.destination)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    assert "추천 교통편" in [s.value for s in app.subheader]
    app.button(key="select_transport_1").click().run()
    assert "선택한 교통편" in [s.value for s in app.subheader]
    expanders = [e for e in app.expander if e.label.startswith("다른 추천 교통편 보기")]
    assert expanders and all(getattr(e, "expanded", False) is False or e.value is False or e.value is None
                             for e in expanders)
