from datetime import date, time
from unittest.mock import Mock

import pytest

from models.access import AccessRoute, AccessStep
from models.trip_request import TripRequest
from providers.http_client import ProviderError
from services.access_service import AccessService
from services.transport_service import TransportService


def setup(origin="구로디지털단지역", destination="구미", **limits):
    places, transit, train, bus, intercity = (Mock() for _ in range(5))
    places.search_addresses.return_value = []
    names = {origin: ("origin", 126.9), "가까운역": ("near", 126.91),
             "먼역": ("far", 127.1), "서울역": ("seoul", 127.0)}
    def search(query, **kwargs):
        if query in names:
            id, x = names[query]
            return [{"id": id, "place_name": query, "x": x, "y": 37.5,
                     "address_name": "서울특별시 구로구", "category_name": "교통 > 지하철역"}]
        return []
    places.search_places.side_effect = search
    train.list_cities.return_value = [{"citycode": "11", "cityname": "서울특별시"},
                                     {"citycode": "47", "cityname": destination}]
    train.list_stations.side_effect = lambda code: ([{"nodeid": "far", "nodename": "먼"},
        {"nodeid": "near", "nodename": "가까운"}, {"nodeid": "seoul", "nodename": "서울"}]
        if code == "11" else [{"nodeid": "dest", "nodename": destination}])
    def schedule(dep, arr, day):
        if dep == "far":
            return []
        name = {"near": "가까운", "seoul": "서울"}[dep]
        return [{"depplacename": name, "arrplacename": destination,
                 "depplandtime": "20261001" + departure,
                 "arrplandtime": "20261001" + arrival}
                for departure, arrival in [("093000", "110000"), ("100000", "130000")]]
    train.search_trips.side_effect = schedule
    bus.list_terminals.return_value = []
    transit.fastest_route.return_value = AccessRoute(duration_seconds=1800, distance_meters=1000,
        transfers=0, steps=(AccessStep(mode="SUBWAY", duration_seconds=1800, distance_meters=1000),))
    trip = TripRequest(departure=origin, destination=destination, start_date=date(2026,10,1),
        end_date=date(2026,10,1), departure_time=time(9), end_time=time(20),
        has_accommodation=False, has_pet=False, has_child=False, activity_radius="500m", preferences=["관광"])
    service = TransportService(train, bus, intercity, places,
        access_service=AccessService(places, transit), today=lambda: date(2026,9,30), **limits)
    return service, trip, places, transit, train, bus


@pytest.mark.parametrize("origin,destination", [("구로디지털단지역", "구미"), ("강남역", "부산"),
                                                ("회사건물", "부산"), ("서울특별시 구로구 123", "부산")])
def test_local_origins_find_real_hubs_and_filter_missed_connections(origin, destination):
    service, trip, places, transit, train, _ = setup(origin, destination)
    result = service.search(trip)
    assert result.origin_status == "LOCAL_ORIGIN"
    assert len(result.candidates) == 2
    assert all(c.departure_time.hour == 10 and c.access.is_feasible for c in result.candidates)
    assert all(c.access.total_duration_minutes == 240 for c in result.candidates)
    assert all(c.arrival_place == destination for c in result.candidates)
    assert all(c.access.access_leg.origin == origin for c in result.candidates)
    assert transit.fastest_route.call_count == 2  # No route call for schedule-less far hub.
    assert sum(call.args[0] == origin for call in places.search_places.call_args_list) == 1
    assert train.search_trips.call_args_list[0].args[0] == "near"


def test_direct_hub_keeps_zero_access_and_buffer():
    service, trip, _, transit, _, _ = setup("서울역")
    result = service.search(trip)
    assert result.origin_status == "DIRECT_HUB"
    assert len(result.candidates) == 2
    assert all(c.access.access_leg.duration_minutes == 0 for c in result.candidates)
    assert all(c.access.buffer_minutes == 15 for c in result.candidates)
    transit.fastest_route.assert_not_called()


def test_invalid_origin_does_not_query_schedules():
    service, trip, places, _, train, _ = setup()
    places.search_places.side_effect = None
    places.search_places.return_value = []
    result = service.search(trip)
    assert result.origin_status == "NEED_ADDRESS"
    assert "도로명 주소 또는 지번 주소" in result.user_message
    places.search_addresses.assert_not_called()
    assert not result.candidates
    train.search_trips.assert_not_called()


def test_limits_and_individual_geocoding_failure():
    service, trip, places, transit, train, _ = setup(local_origin_hub_scan_limit=2, local_origin_hub_limit=1)
    result = service.search(trip)
    assert len(result.candidates) == 1
    assert train.search_trips.call_count == 1
    assert transit.fastest_route.call_count == 1
    assert places.search_places.call_count <= 4  # origin + destination context + two hub lookups


def test_failed_hub_resolution_keeps_other_hub():
    """Unresolved hub POI drops that hub; other hubs with schedules remain."""
    service, trip, places, _, _, _ = setup()
    original = places.search_places.side_effect

    def search(query, **kwargs):
        if query == "가까운역":
            return []
        return original(query, **kwargs)

    places.search_places.side_effect = search
    result = service.search(trip)
    assert len(result.candidates) == 1
    assert result.candidates[0].departure_place == "서울"


def test_transit_quota_keeps_outbound_with_estimate():
    """Kakao Mobility quota must not zero-out TAGO outbound candidates."""
    service, trip, _, transit, _, _ = setup()
    transit.fastest_route.side_effect = ProviderError("http_400_quota")
    result = service.search(trip)
    assert result.candidates
    assert all(c.access and c.access.is_feasible for c in result.candidates)
    assert any(c.access.access_leg.provider == "ESTIMATED" and c.access.access_leg.estimated
               for c in result.candidates)


def test_expands_to_farther_hub_when_nearby_has_no_schedule():
    """Nearest hubs without long-distance service must not hide reachable major hubs."""
    service, trip, places, transit, train, _ = setup()
    # Near hub has no trips; Seoul (farther) still does — same pattern as Yangjae → Yeongdeungpo.
    def schedule(dep, arr, day):
        if dep != "seoul":
            return []
        return [{"depplacename": "서울", "arrplacename": "구미",
                 "depplandtime": "20261001100000", "arrplandtime": "20261001130000"}]
    train.search_trips.side_effect = schedule
    result = service.search(trip)
    assert result.candidates
    assert {c.departure_place for c in result.candidates} == {"서울"}
    assert train.search_trips.call_count >= 2  # nearby probe + expanded Seoul
    assert all(c.access and c.access.is_feasible for c in result.candidates)


def test_failed_bus_keeps_train():
    service, trip, _, _, _, bus = setup()
    bus.list_terminals.side_effect = ProviderError("http_503")
    assert service.search(trip).candidates


def test_ambiguous_origin_rejected_and_subway_suffix_supported():
    service, _, places, _, _, _ = setup()
    places.search_places.side_effect = None
    row = {"id": "one", "place_name": "강남역 2호선", "x": 127, "y": 37,
           "category_name": "교통 > 지하철역"}
    places.search_places.return_value = [row]
    assert service.access_service.resolve_point("강남역").id == "one"
    places.search_places.return_value = [row, dict(row, id="two")]
    with pytest.raises(ProviderError):
        service.access_service.resolve_point("강남역")


def test_local_origin_selection_connects_to_places(monkeypatch):
    from streamlit.testing.v1 import AppTest
    from models.access import AccessPoint
    from models.place import PlaceResult
    from models.trip_request import Preference
    from services.place_service import convert_place
    service, trip, _, _, _, _ = setup()
    result = service.search(trip)
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=result))
    anchor = AccessPoint(id="gumi", name="구미역", x=128.33, y=36.12)
    place = convert_place({"id": "p1", "place_name": "검증 장소", "x": 128.33, "y": 36.12}, anchor, Preference.SIGHTSEEING)
    search = Mock(return_value=PlaceResult(anchor=anchor, candidates=[place], radius_meters=500, status="정상"))
    monkeypatch.setattr("services.place_service.search_places_for_trip", search)
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "false")
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value(trip.departure)
    app.text_input[1].set_value(trip.destination)
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.button(key="search_nearby_places").click().run()
    assert not app.exception
    assert search.call_args.args[1] == result.candidates[0]
    assert search.call_args.args[1].arrival_place == "구미"
    assert app.session_state.place_candidates == [place]
    assert any("검증 장소" in item.value for item in app.markdown)
    assert not any("LOCAL_ORIGIN" in item.value for item in app.caption)


def test_config_limits_validation():
    from config import load_settings
    settings = load_settings(environ={"LOCAL_ORIGIN_HUB_SCAN_LIMIT": "2", "LOCAL_ORIGIN_HUB_LIMIT": "1"})
    assert (settings.local_origin_hub_scan_limit, settings.local_origin_hub_limit) == (2, 1)
    settings = load_settings(environ={"LOCAL_ORIGIN_HUB_SCAN_LIMIT": "0", "LOCAL_ORIGIN_HUB_LIMIT": "invalid"})
    assert (settings.local_origin_hub_scan_limit, settings.local_origin_hub_limit) == (12, 3)


def test_local_ranking_includes_wait_and_access():
    service, trip, _, _, train, _ = setup()
    schedules = train.search_trips.side_effect
    def changed(dep, arr, day):
        rows = schedules(dep, arr, day)
        if dep == "seoul":
            for row in rows:
                row["depplandtime"] = "20261001110000"
                row["arrplandtime"] = "20261001123000"
        return rows
    train.search_trips.side_effect = changed
    result = service.search(trip)
    assert [c.departure_place for c in result.candidates] == ["서울", "가까운"]
    assert [c.access.total_duration_minutes for c in result.candidates] == [210, 240]
    assert result.candidates[0].access.waiting_minutes == 75


def test_kakao_outage_is_not_invalid_input():
    service, trip, places, _, train, _ = setup()
    places.search_places.side_effect = ProviderError("timeout")
    result = service.search(trip)
    assert result.origin_status == "ORIGIN_UNAVAILABLE"
    assert "잠시 후" in result.user_message
    train.search_trips.assert_not_called()
