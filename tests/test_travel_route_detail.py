"""TravelLeg route_detail preservation — Kakao steps on ScheduleItem / Timeline."""
from __future__ import annotations

from datetime import datetime, timedelta

from models.access import AccessPoint, AccessRoute, AccessStep
from models.schedule import ScheduleItem
from models.transport import KST
from services.kakao_route_steps import parse_access_step
from services.route_detail_display import meaningful_route_steps, route_step_lines
from services.schedule_day_view import TimelineEvent, _travel_label


def _steps() -> tuple[AccessStep, ...]:
    return (
        AccessStep(mode="WALKING", duration_seconds=180, distance_meters=200,
                   start_name="마산역", end_name="마산역 정류장"),
        AccessStep(mode="BUS", duration_seconds=780, distance_meters=3200,
                   line_name="100번", start_name="마산역 정류장", end_name="중앙시장"),
        AccessStep(mode="WALKING", duration_seconds=360, distance_meters=400,
                   start_name="중앙시장", end_name="동경복집"),
    )


def test_parse_step_preserves_bus_line_and_stops():
    raw = {
        "properties": {
            "type": "BUS", "time": 1158, "distance": 4127,
            "guidance": "마을 76 (판교역동편 > 성남시청전면)",
            "stops": [{"name": "판교역동편"}, {"name": "성남시청전면"}],
            "vehicles": [{"name": "76", "type": "마을"}],
        }
    }
    step = parse_access_step(raw)
    assert step is not None
    assert step.mode == "BUS"
    assert step.line_name == "76"
    assert step.start_name == "판교역동편"
    assert step.end_name == "성남시청전면"
    assert "100번" not in step.line_name  # never invent


def test_parse_step_omits_missing_line_without_inventing():
    raw = {"properties": {"type": "WALKING", "time": 180, "distance": 200, "guidance": ""}}
    step = parse_access_step(raw)
    assert step is not None
    assert step.mode == "WALKING"
    assert step.line_name == ""
    assert step.start_name == ""
    lines = route_step_lines(step)
    assert lines[0].startswith("도보")
    assert not any("번" in line for line in lines)


def test_parse_subway_vehicle_name():
    raw = {
        "properties": {
            "type": "SUBWAY", "time": 1020, "distance": 8000,
            "stops": [{"name": "가산디지털단지"}, {"name": "노량진"}],
            "vehicles": [{"name": "1호선", "type": "SUBWAY"}],
        }
    }
    step = parse_access_step(raw)
    assert step.mode == "SUBWAY"
    assert step.line_name == "1호선"
    assert "가산디지털단지 → 노량진" in "\n".join(route_step_lines(step))


def test_schedule_item_stores_route_steps_optional():
    origin = AccessPoint(id="a", name="마산역", x=128.57, y=35.18)
    dest = AccessPoint(id="b", name="동경복집", x=128.58, y=35.19)
    start = datetime(2026, 10, 1, 12, 41, tzinfo=KST)
    item = ScheduleItem(
        item_type="TRAVEL", place_id="b", place_name="동경복집",
        start_datetime=start, end_datetime=start + timedelta(minutes=22),
        origin=origin, destination=dest, travel_mode=("BUS", "WALKING"),
        travel_duration_minutes=22, route_steps=_steps(), reason="경로 API 예상 이동시간")
    assert len(item.route_steps) == 3
    bare = item.model_copy(update={"route_steps": ()})
    assert bare.route_steps == ()


def test_case1_2_timeline_travel_keeps_compact_and_detail_steps():
    origin = AccessPoint(id="a", name="마산역", x=128.57, y=35.18)
    dest = AccessPoint(id="b", name="동경복집", x=128.58, y=35.19)
    start = datetime(2026, 10, 1, 12, 41, tzinfo=KST)
    item = ScheduleItem(
        item_type="TRAVEL", place_id="b", place_name="동경복집",
        start_datetime=start, end_datetime=start + timedelta(minutes=22),
        origin=origin, destination=dest, travel_mode=("BUS", "WALKING"),
        travel_duration_minutes=22, route_steps=_steps())
    label = _travel_label(item)
    assert "버스" in label and "도보" in label
    event = TimelineEvent(
        kind="TRAVEL", start=item.start_datetime, end=item.end_datetime,
        title=f"{origin.name} → {dest.name}",
        detail=f"{label} · 약 22분",
        route_steps=item.route_steps)
    assert "약 22분" in event.detail
    steps = meaningful_route_steps(event.route_steps)
    assert [s.mode for s in steps] == ["WALKING", "BUS", "WALKING"]
    assert steps[1].line_name == "100번"


def test_case7_no_route_steps_still_builds_compact_event():
    origin = AccessPoint(id="a", name="마산역", x=128.57, y=35.18)
    dest = AccessPoint(id="b", name="동경복집", x=128.58, y=35.19)
    start = datetime(2026, 10, 1, 12, 41, tzinfo=KST)
    item = ScheduleItem(
        item_type="TRAVEL", place_id="b", place_name="동경복집",
        start_datetime=start, end_datetime=start + timedelta(minutes=22),
        origin=origin, destination=dest, travel_mode=("BUS",),
        travel_duration_minutes=22)
    assert item.route_steps == ()
    assert meaningful_route_steps(item.route_steps) == []


def test_provider_parses_enriched_payload(monkeypatch):
    from unittest.mock import Mock
    from providers.kakao_transit_provider import KakaoTransitProvider, clear_route_cache

    clear_route_cache()
    http = Mock()
    http.request_json.return_value = {
        "status": "OK",
        "routes": [{
            "properties": {"totalTime": 1320, "totalDistance": 3800, "transfers": 0},
            "steps": [
                {"properties": {
                    "type": "WALKING", "time": 180, "distance": 200,
                    "stops": [{"name": "마산역"}, {"name": "마산역 정류장"}],
                }},
                {"properties": {
                    "type": "BUS", "time": 780, "distance": 3200,
                    "vehicles": [{"name": "100번", "type": "BUS"}],
                    "stops": [{"name": "마산역 정류장"}, {"name": "중앙시장"}],
                }},
                {"properties": {
                    "type": "WALKING", "time": 360, "distance": 400,
                    "stops": [{"name": "중앙시장"}, {"name": "동경복집"}],
                }},
            ],
        }],
    }
    origin = AccessPoint(id="1", name="마산역", x=128.57, y=35.18)
    dest = AccessPoint(id="2", name="동경복집", x=128.58, y=35.19)
    route = KakaoTransitProvider("test-key-detail", http).fastest_route(origin, dest)
    assert isinstance(route, AccessRoute)
    assert route.steps[1].line_name == "100번"
    assert route.steps[1].start_name == "마산역 정류장"
    assert http.request_json.call_count == 1
    # Cache hit — no second API call
    again = KakaoTransitProvider("test-key-detail", http).fastest_route(origin, dest)
    assert again.steps[1].line_name == "100번"
    assert http.request_json.call_count == 1
    clear_route_cache()


def test_schedule_service_attaches_route_steps():
    from datetime import date, time
    from models.trip_request import TripRequest
    from services.transport_service import convert_candidate
    from models.transport import TransportType
    from test_schedule import scheduler, candidates

    trip = TripRequest(
        departure="서울역", destination="구미", start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1), departure_time=time(9), end_time=time(20),
        has_accommodation=False, has_pet=False, has_child=False, allergies=[],
        activity_radius="500m", preferences=["맛집", "관광"])
    selected = convert_candidate(
        {"depplacename": "서울", "arrplacename": "구미",
         "depplandtime": "20261001090000", "arrplandtime": "20261001120000"},
        TransportType.TRAIN)
    anchor = AccessPoint(id="station", name="구미역", x=128.33, y=36.12)
    service, transit = scheduler(anchor)
    values = candidates(count=2)
    route = transit.fastest_route.return_value
    rich = route.model_copy(update={"steps": _steps()})
    transit.fastest_route.return_value = rich
    result = service.generate(trip, selected, values, values[0].place_id)
    travels = [i for i in result.schedule.items if i.item_type == "TRAVEL"]
    assert travels
    assert travels[0].route_steps
    assert travels[0].travel_duration_minutes == rich.duration_seconds / 60
