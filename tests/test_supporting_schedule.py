from unittest.mock import Mock
from dataclasses import replace

import pytest

from config import ScheduleSettings
from models.trip_request import Preference
from providers.http_client import ProviderError
from test_schedule import scheduler, candidates
from test_places import trip, selected, anchor


@pytest.mark.parametrize("outcome", ["success", "empty", "outage", "return_too_long"])
def test_arrival_gap_supporting_search_and_fixed_dinner(trip, selected, anchor, outcome):
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=13, minute=25)})
    main = candidates(meal=True, count=1)[0]
    support = candidates(count=2)[1].model_copy(update={"address": "검증 주소", "matched_preferences": (Preference.REST,), "category": "음식점 > 카페"})
    search = Mock(return_value=[] if outcome == "empty" else [support])
    if outcome == "outage":
        search.side_effect = ProviderError("timeout")
    service, transit = scheduler(anchor, supporting_search=search, config=replace(ScheduleSettings(), late_lunch_end_hour=15))
    route = transit.fastest_route.return_value
    def routing(a, b):
        seconds = 2460 if a.id == anchor.id and b.id == main.place_id else 600
        if outcome == "return_too_long" and a.id == support.place_id:
            seconds = 20000
        return route.model_copy(update={"duration_seconds": seconds})
    transit.fastest_route.side_effect = routing
    result = service.generate(trip, selected, [main], main.place_id)
    assert result.schedule and result.schedule.validation_status == "VALIDATED"
    assert search.call_args_list[0].args[2] == anchor
    assert Preference.REST in search.call_args_list[0].args[0].preferences
    visits = [i for i in result.schedule.items if i.item_type != "TRAVEL"]
    dinner = next(i for i in visits if i.place_id == main.place_id)
    assert dinner.start_datetime.hour == 17
    if outcome == "success":
        assert visits[0].place_id == support.place_id
        assert visits[0].end_datetime <= dinner.start_datetime
        assert any(c.args[0].id == support.place_id and c.args[1].id == main.place_id for c in transit.fastest_route.call_args_list)
    elif outcome == "return_too_long":
        assert visits[0].place_id == main.place_id
    else:
        assert len(visits) == 1
    assert transit.fastest_route.call_count <= service.config.max_route_calls


def test_late_lunch_window(trip, selected, anchor):
    service, _ = scheduler(anchor)
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=14, minute=15)})
    result = service.generate(trip, selected, candidates(meal=True, count=1))
    meal = next(i for i in result.schedule.items if i.item_type == "MEAL")
    assert meal.start_datetime.hour == 14 and meal.meal_slot.endswith("점심")


def test_short_gap_skips_search(trip, selected, anchor):
    search = Mock(return_value=[])
    service, _ = scheduler(anchor, supporting_search=search,
                           config=replace(ScheduleSettings(), min_supporting_activity_gap_minutes=240))
    result = service.generate(trip, selected, candidates())
    assert result.schedule
    search.assert_not_called()


def test_supporting_invalid_fields_and_companion_conflict(trip, selected, anchor):
    from models.place import CompanionStatus
    values = candidates()
    bad = values[0].model_copy(update={"place_id": "unsupported", "address": "검증 주소", "pet_status": CompanionStatus.NOT_SUPPORTED})
    missing = values[1].model_copy(update={"place_id": "missing-address", "address": ""})
    service, _ = scheduler(anchor, supporting_search=Mock(return_value=[bad, missing]))
    result = service.generate(trip, selected, [values[2]])
    assert result.schedule
    assert all(i.place_id == values[2].place_id for i in result.schedule.items)


def test_compact_transport_persists(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from test_local_origin import setup
    service, trip, *_ = setup()
    result = service.search(trip)
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=result))
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip.departure)
    app.text_input[1].set_value(trip.destination)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    assert len([b for b in app.button if b.key and b.key.startswith("select_transport_")]) == len(result.candidates)
    app.button(key="select_transport_1").click().run()
    for _ in range(2):
        toggle = next(e for e in app.expander if e.label.startswith("다른 교통편"))
        assert toggle.proto.expanded is False
        assert not any(b.key == "select_transport_1" for b in app.button)
        assert any(b.key == "select_transport_2" for b in toggle.button)
        app.run()
    app.session_state.trip_schedule = "stale"
    app.button(key="select_transport_2").click().run()
    assert app.session_state.selected_transport == result.candidates[1]
    assert "trip_schedule" not in app.session_state
    assert not app.exception
