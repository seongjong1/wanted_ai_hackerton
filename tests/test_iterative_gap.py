from dataclasses import replace
from unittest.mock import Mock

import pytest

from config import ScheduleSettings
from models.trip_request import Preference
from services import schedule_service as module
from test_schedule import scheduler, candidates
from test_places import trip, selected, anchor


def scenario(trip, selected, anchor, iterations=4):
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=14, minute=39)})
    main = candidates(meal=True, count=1)[0]
    cafe = candidates(count=2)[1].model_copy(update={"place_id": "cafe", "address": "검증 주소",
        "category": "카페", "matched_preferences": (Preference.REST,)})
    tour = candidates(count=3)[2].model_copy(update={"place_id": "tour", "address": "검증 주소"})
    duplicate = cafe.model_copy(update={"place_id": "another-cafe"})
    # Destination-level support pool (same set regardless of gap anchor).
    search = Mock(return_value=[cafe, duplicate, tour])
    service, transit = scheduler(anchor, supporting_search=search,
        config=replace(ScheduleSettings(), late_lunch_end_hour=15, max_gap_fill_iterations=iterations))
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a,b: route.model_copy(update={"duration_seconds":
        2460 if a.id == anchor.id and b.id == main.place_id else 600})
    return service, trip, selected, main, search, transit


@pytest.mark.parametrize("iterations", [1, 4])
def test_repeat_diversity_and_iteration_limit(trip, selected, anchor, iterations):
    service, trip, selected, main, search, transit = scenario(trip, selected, anchor, iterations)
    result = service.generate(trip, selected, [main], main.place_id)
    visits = [i for i in result.schedule.items if i.item_type != "TRAVEL"]
    before_dinner = [i for i in visits if i.start_datetime.hour < 17]
    # Long pre-dinner gap prefers enrichment (tourism) over cafe stacking.
    assert before_dinner[0].place_id == "tour"
    assert before_dinner[0].end_datetime.strftime("%H:%M") == "15:49"
    if iterations == 1:
        assert [i.place_id for i in before_dinner] == ["tour"]
    else:
        # Remaining gap may add cafe after tourism; never a second cafe when food-only.
        assert "tour" in {i.place_id for i in before_dinner}
        assert "another-cafe" not in {i.place_id for i in before_dinner}
        dinner = next(i for i in visits if i.place_id == main.place_id)
        last_before = before_dinner[-1]
        assert (dinner.start_datetime - last_before.end_datetime).total_seconds() >= 15 * 60
    assert search.call_count >= 1
    assert transit.fastest_route.call_count <= service.config.max_route_calls


def test_validator_rolls_back_and_tries_next_candidate(monkeypatch, trip, selected, anchor):
    service, trip, selected, main, _, _ = scenario(trip, selected, anchor)
    validate = module.validate_schedule
    rejected = []
    def rejecting(schedule, *args):
        if any(i.place_id == "tour" for i in schedule.items):
            rejected.append(schedule)
            return False
        return validate(schedule, *args)
    monkeypatch.setattr(module, "validate_schedule", rejecting)
    result = service.generate(trip, selected, [main], main.place_id)
    assert rejected and result.schedule.validation_status == "VALIDATED"
    ids = {i.place_id for i in result.schedule.items}
    assert "tour" not in ids
    # Food-only trips may use any single cafe after tourism rollback (quality > density).
    assert "cafe" in ids or "another-cafe" in ids
    assert not ({"cafe", "another-cafe"} <= ids)


def test_rest_preference_allows_second_cafe_after_rollback(monkeypatch, trip, selected, anchor):
    service, trip, selected, main, _, _ = scenario(trip, selected, anchor)
    trip = trip.model_copy(update={"preferences": (Preference.FOOD, Preference.REST)})
    service._user_preferences = (Preference.FOOD, Preference.REST)
    validate = module.validate_schedule
    def rejecting(schedule, *args):
        if any(i.place_id == "tour" for i in schedule.items):
            return False
        return validate(schedule, *args)
    monkeypatch.setattr(module, "validate_schedule", rejecting)
    result = service.generate(trip, selected, [main], main.place_id)
    ids = {i.place_id for i in result.schedule.items}
    assert "tour" not in ids and "another-cafe" in ids


def test_default_anchor_is_not_explicit_schedule_choice(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from models.place import PlaceResult
    from test_local_origin import setup
    service, trip, *_ = setup()
    transports = service.search(trip)
    places = candidates()
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=transports))
    monkeypatch.setattr("services.place_service.search_places_for_trip", Mock(return_value=PlaceResult(
        candidates=places, destination_scope="구미")))
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip.departure)
    app.text_input[1].set_value(trip.destination)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.button(key="search_nearby_places").click().run()
    assert "schedule_preferred_place_id" not in app.session_state
    app.button(key="search_additional_places").click().run()
    assert "schedule_preferred_place_id" not in app.session_state
    # Explicit nearby-point change (or MAIN "이 장소 중심으로") marks a schedule preference.
    app.selectbox(key="nearby_point_choice").set_value(places[1].place_id).run()
    assert app.session_state.schedule_preferred_place_id == places[1].place_id
    assert not app.exception


def test_middle_day_large_lunch_dinner_gap_is_filled(trip, selected, anchor):
    """A 4h+ lunch→dinner gap must run supporting search and insert when candidates exist."""
    from datetime import datetime, time, timedelta
    from models.access import AccessPoint
    from models.transport import KST

    stay = AccessPoint(id="stay", name="임시 숙소", x=128.34, y=36.13)
    day = trip.start_date + timedelta(days=1)
    start = datetime.combine(day, time(9), KST)
    deadline = datetime.combine(day, time(21), KST)
    selected = selected.model_copy(update={"arrival_time": start})
    trip = trip.model_copy(update={
        "preferences": (Preference.FOOD,),
        "end_date": day,
        "end_time": time(21),
        "has_accommodation": True,
    })
    lunch = candidates(meal=True, count=1)[0].model_copy(update={
        "place_id": "lunch", "place_name": "점심 식당", "address": "검증", "preference_score": 90})
    dinner = candidates(meal=True, count=2)[1].model_copy(update={
        "place_id": "dinner", "place_name": "저녁 식당", "address": "검증", "preference_score": 80})
    tour = candidates(count=3)[2].model_copy(update={
        "place_id": "tour", "place_name": "관광지", "address": "검증",
        "matched_preferences": (Preference.SIGHTSEEING,), "category": "여행 > 관광명소"})
    cafe = candidates(count=2)[1].model_copy(update={
        "place_id": "cafe2", "place_name": "카페", "address": "검증",
        "matched_preferences": (Preference.REST,), "category": "카페"})
    search = Mock(side_effect=lambda t, s, p: [tour, cafe])
    service, transit = scheduler(
        stay, supporting_search=search,
        config=replace(ScheduleSettings(), late_lunch_end_hour=15, max_gap_fill_iterations=6))
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a, b: route.model_copy(update={"duration_seconds": 600})
    result = service.generate_window(
        trip, selected, [lunch, dinner], start=start, deadline=deadline, start_point=stay,
        end_point=stay)
    assert result.schedule and result.schedule.validation_status == "VALIDATED"
    visits = [i for i in result.schedule.items if i.item_type != "TRAVEL"]
    ids = [i.place_id for i in visits]
    assert "lunch" in ids and "dinner" in ids
    assert "tour" in ids or "cafe2" in ids
    lunch_item = next(i for i in visits if i.place_id == "lunch")
    dinner_item = next(i for i in visits if i.place_id == "dinner")
    between = [i for i in visits if lunch_item.end_datetime <= i.start_datetime < dinner_item.start_datetime]
    assert between, "supporting activity should occupy part of the lunch–dinner gap"
    occupied = sum((i.end_datetime - i.start_datetime).total_seconds() for i in between)
    span = (dinner_item.start_datetime - lunch_item.end_datetime).total_seconds()
    assert occupied >= 30 * 60
    assert span - occupied < 4 * 3600
    assert any(i.reason == "숙소 이동" or (i.destination and i.destination.id == stay.id)
               for i in result.schedule.items if i.item_type == "TRAVEL")
