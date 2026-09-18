"""Phase 5 — schedule day view / map visualization (no external API)."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from unittest.mock import Mock

import pytest

from models.access import AccessPoint, AccessLeg
from models.place import PlaceCandidate
from models.schedule import (
    ReturnJourney, ReturnStatus, ScheduleItem, TripDaySchedule, TripSchedule)
from models.transport import KST, TransportType
from models.trip_request import Preference
from services.schedule_day_view import (
    build_day_view, build_schedule_views, lodging_point_for_day, lodging_ui_label,
    simplify_category, collect_coordinates)
from services.schedule_map import build_day_deck
from test_places import trip, selected, anchor
from test_multiday_schedule import lodging


def _visit(place_id, name, start, end, *, meal=None, preference="관광", main=False):
    return ScheduleItem(
        item_type="MEAL" if meal else "PLACE", place_id=place_id, place_name=name,
        start_datetime=start, end_datetime=end, preference=preference,
        meal_slot=f"{start.date()}:{meal}" if meal else None,
        reason=(f"{meal} · " if meal else "") + "여행 > 관광명소 · 관광 성향 후보",
        checks=("영업시간 확인 필요",), validation_status="VALIDATED")


def _travel(place_id, name, start, end, origin, destination):
    return ScheduleItem(
        item_type="TRAVEL", place_id=place_id, place_name=name,
        start_datetime=start, end_datetime=end, origin=origin, destination=destination,
        travel_mode=("BUS",), travel_duration_minutes=(end - start).total_seconds() / 60,
        reason="", validation_status="VALIDATED")


def _point(pid, name, x, y):
    return AccessPoint(id=pid, name=name, x=x, y=y, address="경북 구미시", source="test")


def multiday_schedule(*, provisional=False, hotel_a=None, hotel_b=None):
    hub = _point("hub", "구미종합터미널", 128.33, 36.12)
    stay_a = hotel_a or lodging()
    stay_b = hotel_b or stay_a
    d0 = datetime(2026, 9, 18, 13, 25, tzinfo=KST)
    d1 = datetime(2026, 9, 19, 9, 0, tzinfo=KST)
    d2 = datetime(2026, 9, 20, 9, 0, tzinfo=KST)
    p1 = _point("p1", "김태주선산곱창", 128.34, 36.121)
    p2 = _point("p2", "공단종합시장", 128.341, 36.122)
    p3 = _point("p3", "꽃돼지식당", 128.342, 36.123)
    p4 = _point("p4", "동락공원", 128.343, 36.124)
    day1_items = (
        _travel("p1", p1.name, d0, d0 + timedelta(minutes=10), hub, p1),
        _visit("p1", p1.name, d0 + timedelta(minutes=10), d0 + timedelta(minutes=70), meal="점심"),
        _travel("p2", p2.name, d0 + timedelta(minutes=70), d0 + timedelta(minutes=85), p1, p2),
        _visit("p2", p2.name, d0 + timedelta(minutes=85), d0 + timedelta(minutes=145), preference="쇼핑"),
        _travel("p3", p3.name, d0 + timedelta(minutes=145), d0 + timedelta(minutes=160), p2, p3),
        _visit("p3", p3.name, d0 + timedelta(minutes=160), d0 + timedelta(minutes=220), meal="저녁"),
        _travel("p4", p4.name, d0 + timedelta(minutes=220), d0 + timedelta(minutes=235), p3, p4),
        _visit("p4", p4.name, d0 + timedelta(minutes=235), d0 + timedelta(minutes=295)),
        _travel(stay_a.id, stay_a.name, d0 + timedelta(minutes=295), d0 + timedelta(minutes=320),
                p4, stay_a),
    )
    day2_items = (
        _travel("p2", p2.name, d1, d1 + timedelta(minutes=15), stay_a, p2),
        _visit("p2", p2.name, d1 + timedelta(minutes=15), d1 + timedelta(minutes=75), preference="쇼핑"),
        _travel(stay_b.id, stay_b.name, d1 + timedelta(minutes=75), d1 + timedelta(minutes=100),
                p2, stay_b),
    )
    day3_items = (
        _travel("p4", p4.name, d2, d2 + timedelta(minutes=20), stay_b, p4),
        _visit("p4", p4.name, d2 + timedelta(minutes=20), d2 + timedelta(minutes=80)),
    )
    days = (
        TripDaySchedule(day_index=1, date=d0.date(), role="FIRST", start_location=hub,
                        end_location=stay_a, activity_start=d0,
                        activity_end=day1_items[-1].end_datetime, items=day1_items),
        TripDaySchedule(day_index=2, date=d1.date(), role="MIDDLE", start_location=stay_a,
                        end_location=stay_b, activity_start=d1,
                        activity_end=day2_items[-1].end_datetime, items=day2_items),
        TripDaySchedule(day_index=3, date=d2.date(), role="FINAL", start_location=stay_b,
                        end_location=stay_b, activity_start=d2,
                        activity_end=day3_items[-1].end_datetime, items=day3_items),
    )
    from models.transport import TransportCandidate
    tc = TransportCandidate(
        transport_type=TransportType.TRAIN, grade="무궁화",
        departure_place="구미", arrival_place="영등포",
        departure_time=datetime(2026, 9, 20, 15, 50, tzinfo=KST),
        arrival_time=datetime(2026, 9, 20, 18, 28, tzinfo=KST),
        price=10000, provider="test")
    hub_leg = AccessLeg(
        origin="동락공원", destination="구미역", transport_modes=("BUS",),
        duration_minutes=20,
        departure_time=day3_items[-1].end_datetime, provider="test",
        origin_point=p4, destination_point=_point("return-hub", "구미역", 128.35, 36.14))
    home_leg = AccessLeg(
        origin="영등포역", destination="서울 금천구", transport_modes=("SUBWAY",),
        duration_minutes=25, departure_time=tc.arrival_time, provider="test")
    journey = ReturnJourney(
        to_hub=hub_leg, transport=tc, boarding_buffer_minutes=15, to_origin=home_leg)
    status = "PROVISIONAL" if provisional else "CONFIRMED"
    stay = hub if provisional else stay_a
    nights = (hub, hub) if provisional else (stay_a, stay_b)
    if provisional:
        days = (
            days[0].model_copy(update={"end_location": hub}),
            days[1].model_copy(update={"start_location": hub, "end_location": hub}),
            days[2].model_copy(update={"start_location": hub, "end_location": hub}),
        )
    return TripSchedule(
        trip_start_datetime=d0,
        trip_end_datetime=datetime(2026, 9, 20, 20, 0, tzinfo=KST),
        arrival_point=hub, items=(), days=days, accommodation=stay,
        accommodation_status=status, accommodation_nights=nights,
        return_journey=journey, return_status=ReturnStatus.RETURN_AVAILABLE,
        final_arrival_datetime=home_leg.arrival_time,
        destination_activity_cutoff=datetime(2026, 9, 20, 15, 6, tzinfo=KST),
        user_selected_place_id="p3", validation_status="VALIDATED")


def test_simplify_category():
    assert "곱창" in simplify_category("음식점 > 한식 > 육류,고기 > 곱창,막창") or \
        "한식" in simplify_category("음식점 > 한식 > 육류,고기 > 곱창,막창")
    assert lodging_ui_label("PROVISIONAL") == "임시 기준점"
    assert lodging_ui_label("CONFIRMED") == "숙소"


def test_case1_2_day_isolation(selected):
    schedule = multiday_schedule()
    views = build_schedule_views(schedule, selected=selected)
    assert len(views) == 3
    day1_ids = {e.place_id for e in views[0].timeline if e.kind == "ACTIVITY"}
    day2_ids = {e.place_id for e in views[1].timeline if e.kind == "ACTIVITY"}
    assert "p1" in day1_ids and "p1" not in day2_ids
    assert all(m.sequence is None or m.sequence >= 1 for m in views[0].markers)


def test_case3_4_5_final_order_and_main(selected):
    schedule = multiday_schedule()
    views = build_schedule_views(schedule, selected=selected)
    day1 = views[0]
    activities = [e for e in day1.timeline if e.kind == "ACTIVITY"]
    assert [e.sequence for e in activities] == [1, 2, 3, 4]
    marker_seq = {m.place_id: m.sequence for m in day1.markers if m.sequence}
    assert marker_seq["p1"] == 1 and marker_seq["p3"] == 3
    main = next(m for m in day1.markers if m.place_id == "p3")
    assert main.is_main and main.role == "MAIN"
    assert any(e.is_main and "★" in e.title for e in activities)
    final = views[2]
    kinds = [e.kind for e in final.timeline]
    assert "RETURN_HUB" in kinds and "RETURN_TRANSPORT" in kinds and "RETURN_DONE" in kinds
    assert final.return_summary and final.return_summary.expected is not None


def test_case6_7_confirmed_vs_provisional(selected):
    confirmed = build_schedule_views(multiday_schedule(provisional=False), selected=selected)[0]
    provisional = build_schedule_views(multiday_schedule(provisional=True), selected=selected)[0]
    assert any(m.role == "ACCOMMODATION" for m in confirmed.markers) or confirmed.lodging_label == "숙소"
    assert provisional.lodging_label == "임시 기준점"
    assert any("임시 기준점" in line for line in provisional.summary.lines)
    assert not any("호텔" in e.title and e.kind == "ARRIVAL" for e in provisional.timeline
                   if "임시" not in e.title and provisional.lodging_name in e.title)


def test_case8_9_10_accommodation_nights(selected):
    stay_a = lodging()
    stay_b = AccessPoint(id="stay-b", name="둘째날호텔", address="경북 구미시", x=128.36, y=36.15)
    schedule = multiday_schedule(hotel_a=stay_a, hotel_b=stay_b)
    # Force different nights: night0=A, night1=B
    schedule = schedule.model_copy(update={"accommodation_nights": (stay_a, stay_b),
                                           "accommodation": stay_a})
    d1, d2, d3 = schedule.days
    assert lodging_point_for_day(schedule, d1, boundary="end").id == stay_a.id
    assert lodging_point_for_day(schedule, d2, boundary="start").id == stay_a.id
    assert lodging_point_for_day(schedule, d2, boundary="end").id == stay_b.id
    assert lodging_point_for_day(schedule, d3, boundary="start").id == stay_b.id
    views = build_schedule_views(schedule, selected=selected)
    assert stay_a.name in " ".join(views[0].summary.lines) or views[0].lodging_name == stay_a.name


def test_case11_missing_coords_still_timeline(selected):
    schedule = multiday_schedule()
    # Remove coords for p2 by using candidates without p2 and stripping travel dest — 
    # activity still in timeline via items; marker omitted when not in collect map.
    views = build_schedule_views(schedule, selected=selected, candidates=[])
    day1 = views[0]
    assert any(e.place_id == "p2" and e.kind == "ACTIVITY" for e in day1.timeline)
    # Markers still present via travel destinations on AccessPoints in items.
    assert build_day_deck(day1) is not None


def test_case12_map_failure_isolation(selected, monkeypatch):
    schedule = multiday_schedule()
    view = build_schedule_views(schedule, selected=selected)[0]
    monkeypatch.setattr("services.schedule_map.pdk", Mock(side_effect=RuntimeError("map boom")), raising=False)
    # build_day_deck imports pydeck inside — patch build to raise
    monkeypatch.setattr("services.schedule_map.build_day_deck", Mock(side_effect=RuntimeError("boom")))
    from services.schedule_visualization import render_day_map
    # Should not raise
    render_day_map(view, key="t")
    assert any(e.kind == "ACTIVITY" for e in view.timeline)


def test_case13_order_line_not_geometry_claim(selected):
    view = build_schedule_views(multiday_schedule(), selected=selected)[0]
    assert len(view.order_line) >= 2
    deck = build_day_deck(view)
    assert deck is not None


def test_path_sequence_day1_hub_to_activity_and_back(selected):
    """CASE A/B/E/F — DAY1 START→① and last→END even when Start/End share coords."""
    schedule = multiday_schedule(provisional=True)
    view = build_schedule_views(schedule, selected=selected)[0]
    roles = [p.role for p in view.path_sequence]
    assert roles[0] == "START"
    assert roles[-1] == "END"
    assert "ACTIVITY" in roles
    # Hub → Activity 1 segment
    assert view.path_sequence[0].place_id == "hub"
    assert view.path_sequence[1].role == "ACTIVITY"
    assert view.path_sequence[1].sequence == 1
    # Last activity → provisional end
    assert view.path_sequence[-2].role == "ACTIVITY"
    assert view.path_sequence[-1].place_id == "hub"
    # Same place_id may appear twice in path; marker list still deduped.
    hub_in_path = sum(1 for p in view.path_sequence if p.place_id == "hub")
    assert hub_in_path == 2
    hub_markers = [m for m in view.markers if m.place_id == "hub"]
    assert len(hub_markers) == 1
    assert len(view.order_line) == len(view.path_sequence)


def test_path_sequence_middle_day_lodge_bookends(selected):
    """CASE C/D — Middle Day lodge → activities → lodge."""
    view = build_schedule_views(multiday_schedule(), selected=selected)[1]
    assert view.path_sequence[0].role == "START"
    assert view.path_sequence[-1].role == "END"
    assert view.path_sequence[1].role == "ACTIVITY"
    assert view.path_sequence[1].sequence == 1
    assert view.path_sequence[-2].role == "ACTIVITY"


def test_path_sequence_final_to_return_hub(selected):
    """CASE G — Final Day ends at Return Hub on destination map."""
    view = build_schedule_views(multiday_schedule(), selected=selected)[2]
    assert view.path_sequence[0].role == "START"
    assert view.path_sequence[-1].role == "RETURN_HUB"
    assert any(p.role == "ACTIVITY" for p in view.path_sequence)


def test_main_marker_label_rows(selected):
    view = build_schedule_views(multiday_schedule(), selected=selected)[0]
    from services.schedule_map import _marker_rows, build_day_deck
    rows = _marker_rows(view.markers)
    main = next(r for r in rows if r["role"] == "MAIN")
    assert main["text"] == "3 MAIN"
    assert "★" not in main["text"]
    assert {r["text"] for r in rows if r["text"] in {"1", "2", "3 MAIN", "4"}} >= {"1", "2", "4"}
    # Hub/lodging use letter codes — never activity numbers.
    assert next(r["text"] for r in rows if r["role"] == "ARRIVAL_HUB") == "H"
    assert next(r["text"] for r in rows if r["role"] == "ACCOMMODATION") == "S"
    deck = build_day_deck(view)
    text_layers = [layer for layer in deck.layers if getattr(layer, "type", "") == "TextLayer"
                   or layer.__dict__.get("@@type") == "TextLayer"]
    assert text_layers
    size_units = text_layers[0].__dict__.get("size_units")
    assert str(size_units) == "pixels"
    assert text_layers[0].__dict__.get("get_size") >= 14


def test_case14_15_free_time_and_outbound(selected):
    schedule = multiday_schedule()
    # Inject a gap on day 2
    day2 = schedule.days[1]
    items = list(day2.items)
    items[0] = items[0].model_copy(update={
        "start_datetime": day2.activity_start + timedelta(minutes=40),
        "end_datetime": day2.activity_start + timedelta(minutes=55),
    })
    items[1] = items[1].model_copy(update={
        "start_datetime": day2.activity_start + timedelta(minutes=55),
        "end_datetime": day2.activity_start + timedelta(minutes=115),
    })
    day2 = day2.model_copy(update={"items": tuple(items)})
    schedule = schedule.model_copy(update={
        "days": (schedule.days[0], day2, schedule.days[2])})
    views = build_schedule_views(schedule, selected=selected)
    mid = views[1].timeline
    assert mid[0].kind == "DAY_START"
    assert "출발" not in mid[0].title
    assert any(e.kind == "FREE_TIME" and "자유시간" in e.title for e in mid)
    free = next(e for e in mid if e.kind == "FREE_TIME")
    assert "숙소에서 자유시간" in free.detail or "휴식/출발 준비" in free.detail
    assert not any(e.kind == "DEPARTURE" for e in mid)
    assert any(e.kind == "OUTBOUND" for e in views[0].timeline)


def test_day_start_equals_first_travel_emits_departure(selected):
    """CASE 3 — first travel at day_start → 숙소 출발, no DAY_START duplicate."""
    view = build_schedule_views(multiday_schedule(), selected=selected)[1]
    assert view.timeline[0].kind == "DEPARTURE"
    assert not any(e.kind == "DAY_START" for e in view.timeline)


def test_provisional_day_start_location_label(selected):
    """CASE 4 — provisional uses 임시 기준점 wording on DAY_START."""
    schedule = multiday_schedule(provisional=True)
    day2 = schedule.days[1]
    items = list(day2.items)
    # Force delayed first travel from hub.
    delayed = day2.activity_start + timedelta(minutes=76)
    items[0] = items[0].model_copy(update={
        "start_datetime": delayed,
        "end_datetime": delayed + timedelta(minutes=20),
        "origin": schedule.accommodation,
    })
    day2 = day2.model_copy(update={"items": tuple(items)})
    schedule = schedule.model_copy(update={"days": (schedule.days[0], day2, schedule.days[2])})
    view = build_schedule_views(schedule, selected=selected)[1]
    assert view.timeline[0].kind == "DAY_START"
    assert "임시 기준점" in view.timeline[0].detail
    free = next(e for e in view.timeline if e.kind == "FREE_TIME")
    assert "임시 기준점" in free.detail


def test_meal_label_not_duplicated():
    """CASE 5/6 — 점심/저녁 appear once in activity detail."""
    from services.schedule_day_view import _activity_detail
    start = datetime(2026, 9, 18, 12, 0, tzinfo=KST)
    lunch = _visit("m1", "식당", start, start + timedelta(minutes=60), meal="점심")
    lunch = lunch.model_copy(update={
        "reason": "점심 · 음식점 > 한식 · 맛집 성향 후보",
        "meal_slot": f"{start.date()}:점심",
    })
    detail = _activity_detail(lunch)
    assert detail.count("점심") == 1
    assert "체류 60분" in detail
    dinner = lunch.model_copy(update={
        "meal_slot": f"{start.date()}:저녁",
        "reason": "저녁 · 음식점 > 한식 · 맛집 성향 후보",
    })
    assert _activity_detail(dinner).count("저녁") == 1


def test_case17_return_margin(selected):
    view = build_schedule_views(multiday_schedule(), selected=selected)[2]
    assert view.return_summary.goal.hour == 20
    assert view.return_summary.expected is not None
    assert view.return_summary.margin_minutes is not None
    assert view.return_summary.margin_minutes > 0


def test_case19_20_21_no_api_imports_in_day_view():
    import services.schedule_day_view as mod
    source = open(mod.__file__, encoding="utf-8").read()
    assert "KakaoProvider" not in source
    assert "KakaoTransitProvider" not in source
    assert "Tago" not in source
    assert "request_json" not in source
    assert "HttpClient" not in source
