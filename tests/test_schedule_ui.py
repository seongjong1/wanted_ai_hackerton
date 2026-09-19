from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from models.place import PlaceResult
from models.access import AccessPoint
from test_local_origin import setup
from test_schedule import scheduler, candidates


@pytest.mark.parametrize("change", ["transport", "conditions", "place", "nearby"])
def test_local_origin_to_schedule_and_invalidation(monkeypatch, change):
    transport_service, trip, _, _, _, _ = setup()
    transport_result = transport_service.search(trip)
    values = candidates()
    anchor = AccessPoint(id="gumi", name="구미역", x=128.33, y=36.12)
    places = PlaceResult(anchor=anchor, candidates=values, destination_scope="구미", status="정상")
    schedule_service, _ = scheduler(anchor)
    generate = Mock(side_effect=lambda trip, selected, values, settings, preferred, accommodation_query=None, **kwargs: schedule_service.generate(trip, selected, values, preferred))
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=transport_result))
    monkeypatch.setattr("services.place_service.search_places_for_trip", Mock(return_value=places))
    monkeypatch.setattr("services.schedule_service.generate_trip_schedule", generate)
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "false")
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip.departure)
    app.text_input[1].set_value(trip.destination)
    app.date_input[0].set_value(trip.start_date)
    app.date_input[1].set_value(trip.end_date)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.button(key="search_nearby_places").click().run()
    app.button(key=f"anchor_schedule_{values[0].place_id}").click().run()
    assert not app.exception
    assert app.session_state.trip_schedule.validation_status == "VALIDATED"
    assert app.session_state.trip_schedule.trip_start_datetime == app.session_state.selected_transport.arrival_time
    app.run()
    assert generate.call_count == 1
    if change == "transport":
        app.button(key="select_transport_2").click().run()
    elif change == "conditions":
        app.time_input[1].set_value(trip.end_time.replace(hour=19))
        app.button[0].click().run()
    elif change == "place":
        app.selectbox(key="nearby_point_choice").set_value(values[1].place_id).run()
    else:
        app.button(key="search_additional_places").click().run()
    assert not app.exception
    assert "trip_schedule" not in app.session_state or change == "place"
    if change == "place":
        assert app.session_state.trip_schedule.items[0].place_id == values[1].place_id
        assert generate.call_count == 2
