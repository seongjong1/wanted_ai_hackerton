from datetime import date, datetime, time
from unittest.mock import Mock

import pytest

from models.access import AccessRoute, AccessStep
from models.transport import KST, TransportType
from models.trip_request import TripRequest
from providers.http_client import ProviderError
from services.access_service import AccessService, AccessCache
from services.transport_service import TransportService, convert_candidate


def trip():
    return TripRequest(departure="서울역", destination="부산", start_date=date(2026,10,1),
                       end_date=date(2026,10,1), departure_time=time(9), end_time=time(20),
                       has_accommodation=False, has_pet=False, has_child=False,
                       activity_radius="500m", preferences=["관광"])


def candidate(hour=9, minute=35, *, train=False):
    row = {"depPlaceNm": "서울경부", "arrPlaceNm": "부산", "depPlandTime": f"20261001{hour:02d}{minute:02d}",
           "arrPlandTime": "202610011300", "charge": 30000, "gradeNm": "우등"}
    if train:
        row.update(depplacename="서울", arrplacename="부산")
    return convert_candidate(row, TransportType.TRAIN if train else TransportType.EXPRESS_BUS)


def point(name, id, category=""):
    return {"id": id, "place_name": name, "x": "127", "y": "37", "category_name": category}


def access_service():
    places, transit = Mock(), Mock()
    places.search_places.side_effect = lambda q, **kw: ([point("서울역", "station")]
        if q == "서울역" else [point("서울고속버스터미널(경부)", "terminal", "교통 > 버스터미널")])
    transit.fastest_route.return_value = AccessRoute(duration_seconds=1680, distance_meters=10000, transfers=1,
        steps=(AccessStep(mode="SUBWAY", duration_seconds=1500, distance_meters=9500),
               AccessStep(mode="WALKING", duration_seconds=180, distance_meters=500)))
    return AccessService(places, transit), places, transit


def test_filter_and_cached_route_for_same_terminal():
    service, places, transit = access_service()
    values, missed, unknown = service.filter_candidates([candidate(), candidate(minute=43), candidate(hour=10)],
                                                        trip(), AccessCache())
    assert (len(values), missed, unknown) == (2, 1, 0)
    assert values[0].access.access_leg.duration_minutes == 28
    assert values[0].access.access_leg.estimated is False
    assert values[0].access.access_leg.provider == "Kakao publictraffic"
    assert "ESTIMATED" not in values[0].access.access_leg.transport_modes
    assert values[0].access.ready_time == datetime(2026,10,1,9,43,tzinfo=KST)
    assert transit.fastest_route.call_count == 1
    assert places.search_places.call_count == 2


@pytest.mark.parametrize("error", ["timeout", "http_429", "http_503", "http_400_quota", "ACCESS_TIME_UNKNOWN"])
def test_transit_outage_uses_estimate_not_zero_outbound(error):
    """Kakao routing outage/quota must not wipe TAGO outbound candidates."""
    service, _, transit = access_service()
    transit.fastest_route.side_effect = ProviderError(error)
    values, missed, unknown = service.filter_candidates([candidate(), candidate(hour=10)], trip(), AccessCache())
    assert unknown == 0 and missed == 0
    assert len(values) == 2
    assert values[0].access.access_leg.provider == "ESTIMATED"
    assert values[0].access.access_leg.estimated is True
    assert values[0].access.access_leg.transport_modes == ("ESTIMATED",)
    assert transit.fastest_route.call_count == 1


def test_same_verified_point_skips_routing_not_buffer():
    service, _, transit = access_service()
    values, missed, _ = service.filter_candidates([candidate(minute=10,train=True), candidate(minute=15,train=True)],
                                                  trip(), AccessCache())
    assert len(values) == 1 and missed == 1
    assert values[0].access.access_leg.duration_minutes == 0
    assert values[0].access.access_leg.estimated is False
    transit.fastest_route.assert_not_called()


def test_aliases_share_route_by_place_ids():
    service, _, transit = access_service()
    cache = AccessCache()
    service.get_leg("서울역", "서울경부버스터미널", datetime(2026,10,1,9,tzinfo=KST), cache)
    service.get_leg("서울역", "서울고속버스터미널(경부)", datetime(2026,10,1,9,tzinfo=KST), cache)
    assert transit.fastest_route.call_count == 1


def test_city_or_ambiguous_place_not_arbitrarily_geocoded():
    service, places, transit = access_service()
    places.search_places.return_value = []
    places.search_places.side_effect = lambda q, **kw: [point("서울역", "1"), point("서울역", "2")]
    with pytest.raises(ProviderError, match="ACCESS_TIME_UNKNOWN"):
        service.resolve_point("서울역")
    with pytest.raises(ProviderError, match="ACCESS_TIME_UNKNOWN"):
        service.resolve_point("서울")
    transit.fastest_route.assert_not_called()


def test_sixth_candidate_survives_filter_before_top_five():
    access, _, _ = access_service()
    train, express, intercity = Mock(), Mock(), Mock()
    train.list_cities.return_value = []
    express.list_terminals.side_effect = lambda q: ([{"terminalId":"S","terminalNm":"서울경부"}]
                                                   if q == "서울" else [{"terminalId":"B","terminalNm":"부산"}])
    express.search_trips.return_value = [candidate(minute=m).raw_data for m in (30,31,32,33,34,50)]
    result = TransportService(train, express, intercity, access_service=access,
                              today=lambda: date(2026,9,30)).search(trip())
    assert result.access_checked
    assert len(result.candidates) == 1
    assert result.candidates[0].departure_time.minute == 50
    assert "탑승 불가 5개" in result.statuses[1].detail


def test_unknown_bus_does_not_remove_train():
    access, _, transit = access_service()
    transit.fastest_route.side_effect = ProviderError("timeout")
    values, _, unknown = access.filter_candidates([candidate(), candidate(train=True)], trip(), AccessCache())
    # Bus uses distance estimate; train is same-place and skips routing.
    assert unknown == 0 and len(values) == 2
    assert {c.transport_type for c in values} == {TransportType.EXPRESS_BUS, TransportType.TRAIN}
