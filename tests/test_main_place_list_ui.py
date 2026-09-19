"""MAIN place list collapse UX — matches transport/accommodation collapse pattern."""
from __future__ import annotations

from unittest.mock import Mock

from models.place import PlaceResult
from models.schedule import ScheduleResult
from streamlit.testing.v1 import AppTest

from test_local_origin import setup
from test_schedule import candidates


def _open_main_list(monkeypatch):
    service, trip, *_ = setup()
    transport = service.search(trip)
    values = candidates()
    generate = Mock(return_value=ScheduleResult("귀가 조건 미충족"))
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=transport))
    monkeypatch.setattr(
        "services.place_service.search_places_for_trip",
        Mock(return_value=PlaceResult(candidates=values, destination_scope="구미")),
    )
    monkeypatch.setattr("services.schedule_service.generate_trip_schedule", generate)
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip.departure)
    app.text_input[1].set_value(trip.destination)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.button(key="search_nearby_places").click().run()
    return app, values, generate


def _anchor_keys(app):
    return {getattr(b, "key", None) for b in app.button}


def test_case1_unselected_list_expanded(monkeypatch):
    app, values, _ = _open_main_list(monkeypatch)
    assert "user_selected_place_id" not in app.session_state
    assert f"anchor_schedule_{values[0].place_id}" in _anchor_keys(app)
    assert "expand_main_place_list" not in _anchor_keys(app)
    assert "구미 주요 추천 장소" in [s.value for s in app.subheader]
    assert not app.exception


def test_case2_3_select_collapses_and_shows_summary(monkeypatch):
    app, values, generate = _open_main_list(monkeypatch)
    target = values[0]
    app.button(key=f"anchor_schedule_{target.place_id}").click().run()
    assert app.session_state.user_selected_place_id == target.place_id
    assert app.session_state.main_place_list_expanded is False
    # Candidate pick buttons hidden; change button present
    assert f"anchor_schedule_{target.place_id}" not in _anchor_keys(app)
    assert "expand_main_place_list" in _anchor_keys(app)
    markdown = " ".join(getattr(m, "value", "") or "" for m in app.markdown)
    assert "선택한 기준 장소" in markdown
    texts = " ".join(getattr(t, "value", "") or "" for t in app.text)
    assert target.place_name in " ".join(
        getattr(w, "value", "") or "" for w in list(app.markdown) + list(app.text)
    ) or any(target.place_name in str(getattr(c, "value", "")) for c in app.markdown)
    # Summary card still shows name via write → often markdown/text
    body = "\n".join(
        str(getattr(el, "value", ""))
        for group in (app.markdown, app.text, app.caption)
        for el in group
    )
    assert target.place_name in body or any(
        target.place_name in str(getattr(b, "label", "")) for b in app.button
    )
    assert generate.call_count >= 1
    assert not app.exception


def test_case4_5_change_reopens_then_new_select_collapses(monkeypatch):
    app, values, generate = _open_main_list(monkeypatch)
    first, second = values[0], values[1]
    app.button(key=f"anchor_schedule_{first.place_id}").click().run()
    assert app.session_state.main_place_list_expanded is False
    app.button(key="expand_main_place_list").click().run()
    assert app.session_state.main_place_list_expanded is True
    assert f"anchor_schedule_{second.place_id}" in _anchor_keys(app)
    before = generate.call_count
    app.session_state.trip_schedule = "stale"
    app.button(key=f"anchor_schedule_{second.place_id}").click().run()
    assert app.session_state.user_selected_place_id == second.place_id
    assert app.session_state.main_place_list_expanded is False
    assert app.session_state.trip_schedule is None  # stale cleared for recalculate
    assert generate.call_count > before
    assert f"anchor_schedule_{second.place_id}" not in _anchor_keys(app)
    assert not app.exception


def test_case6_rerun_keeps_collapsed(monkeypatch):
    app, values, _ = _open_main_list(monkeypatch)
    app.button(key=f"anchor_schedule_{values[0].place_id}").click().run()
    assert app.session_state.main_place_list_expanded is False
    app.run()
    assert app.session_state.user_selected_place_id == values[0].place_id
    assert app.session_state.main_place_list_expanded is False
    assert "expand_main_place_list" in _anchor_keys(app)
    assert f"anchor_schedule_{values[0].place_id}" not in _anchor_keys(app)
    assert not app.exception


def test_case7_stale_main_missing_from_new_search_expands(monkeypatch):
    service, trip, *_ = setup()
    transport = service.search(trip)
    first_pool = candidates()
    # Second search drops previous MAIN id
    second_pool = [
        c.model_copy(update={"place_id": f"new-{i}", "place_name": f"새장소{i}"})
        for i, c in enumerate(first_pool[:3])
    ]
    search = Mock(side_effect=[
        PlaceResult(candidates=first_pool, destination_scope="구미"),
        PlaceResult(candidates=second_pool, destination_scope="구미"),
    ])
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=transport))
    monkeypatch.setattr("services.place_service.search_places_for_trip", search)
    monkeypatch.setattr(
        "services.schedule_service.generate_trip_schedule",
        Mock(return_value=ScheduleResult("귀가 조건 미충족")),
    )
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip.departure)
    app.text_input[1].set_value(trip.destination)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.button(key="search_nearby_places").click().run()
    app.button(key=f"anchor_schedule_{first_pool[0].place_id}").click().run()
    assert app.session_state.main_place_list_expanded is False
    app.button(key="search_nearby_places").click().run()
    assert "user_selected_place_id" not in app.session_state
    assert app.session_state.main_place_list_expanded is True
    assert f"anchor_schedule_{second_pool[0].place_id}" in _anchor_keys(app)
    assert not app.exception


def test_case8_transport_collapse_regression(monkeypatch):
    """Transport still collapses after select (existing compact UX)."""
    from models.transport import TransportResult, TransportStatus, TransportType
    from datetime import timedelta
    service, trip, *_ = setup()
    base = service.search(trip).candidates[0]
    rows = [
        base.model_copy(update={
            "train_number": str(i),
            "departure_time": base.departure_time + timedelta(minutes=i),
            "arrival_time": base.arrival_time + timedelta(minutes=i),
        })
        for i in range(5)
    ]
    result = TransportResult(
        candidates=rows,
        by_type={TransportType.TRAIN: rows},
        statuses=[TransportStatus(TransportType.TRAIN, "정상", "")],
        origin_status="LOCAL_ORIGIN",
    )
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=result))
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip.departure)
    app.text_input[1].set_value(trip.destination)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    assert "선택한 교통편" in [s.value for s in app.subheader]
    expanders = [e for e in app.expander if str(getattr(e, "label", "")).startswith("다른 교통편 보기")]
    assert expanders
    assert not app.exception
