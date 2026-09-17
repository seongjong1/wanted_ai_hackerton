from unittest.mock import Mock

import pytest

from models.trip_request import Preference
from test_schedule import scheduler, candidates
from test_places import trip, selected, anchor


@pytest.mark.parametrize("preferred", [None, "0"])
def test_actual_route_overrides_main_rank_but_respects_explicit_choice(trip, selected, anchor, preferred):
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=13, minute=25)})
    values = candidates(meal=True, count=3)
    values[0] = values[0].model_copy(update={"preference_score": 100})
    values[1] = values[1].model_copy(update={"preference_score": 95})
    values[2] = values[2].model_copy(update={"preference_score": 90})
    original = list(values)
    service, transit = scheduler(anchor)
    route = transit.fastest_route.return_value
    transit.fastest_route.side_effect = lambda a,b: route.model_copy(update={"duration_seconds":
        {"0": 2460, "1": 2100, "2": 600}[b.id] if a.id == anchor.id else 600})
    result = service.generate(trip, selected, values, preferred)
    assert result.schedule.items[0].place_id == (preferred or "2")
    assert result.schedule.items[0].duration_minutes == (41 if preferred else 10)
    assert values == original


def test_next_anchor_search_and_actual_detour(trip, selected, anchor):
    from dataclasses import replace
    from config import ScheduleSettings
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,), "activity_radius": "500m 이상"})
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=13, minute=25)})
    main = candidates(meal=True, count=1)[0]
    a = candidates(count=2)[1].model_copy(update={"place_id": "detour", "address": "검증 주소",
        "category": "카페", "matched_preferences": (Preference.REST,), "role": "MAIN_DESTINATION"})
    b = a.model_copy(update={"place_id": "on-route", "latitude": a.latitude + .001})
    # Destination-level support pool returns both; actual detour cost selects on-route.
    search = Mock(return_value=[a, b])
    service, transit = scheduler(anchor, supporting_search=search,
        config=replace(ScheduleSettings(), late_lunch_end_hour=15, max_gap_fill_iterations=1))
    route = transit.fastest_route.return_value
    def routing(origin, target):
        minutes = 41 if origin.id == anchor.id and target.id == main.place_id else 10
        if origin.id == "detour": minutes = 60
        return route.model_copy(update={"duration_seconds": minutes*60})
    transit.fastest_route.side_effect = routing
    result = service.generate(trip, selected, [main], main.place_id)
    visits = [i for i in result.schedule.items if i.item_type != "TRAVEL"]
    assert visits[0].place_id == "on-route"
    assert search.call_count >= 1
    assert visits[-1].start_datetime.hour == 17


def test_filled_meal_slots_do_not_consume_supporting_route_budget(trip, selected, anchor):
    trip = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    selected = selected.model_copy(update={"arrival_time": selected.arrival_time.replace(hour=13, minute=25)})
    service, transit = scheduler(anchor)
    result = service.generate(trip, selected, candidates(meal=True, count=8))
    assert len([i for i in result.schedule.items if i.item_type == "MEAL"]) == 2
    assert transit.fastest_route.call_count <= 6
