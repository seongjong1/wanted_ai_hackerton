from unittest.mock import Mock
from datetime import date

import pytest

from test_local_origin import setup
from services.access_service import AccessService
from services.transport_service import convert_candidate, rank_candidates
from models.transport import TransportType
from providers.http_client import ProviderError


@pytest.mark.parametrize("station", ["서울역", "영등포역", "용산역"])
def test_exact_station_outranks_subway_lines(station):
    places = Mock()
    places.search_places.return_value = [
        {"id":"subway","place_name":station+" 1호선","category_name":"교통 > 지하철역","x":127,"y":37},
        {"id":"rail","place_name":station,"category_name":"교통 > 기차역","x":127,"y":37}]
    assert AccessService(places, Mock()).resolve_point(station).id == "rail"
    places.search_places.return_value.append(dict(places.search_places.return_value[1],id="other"))
    with pytest.raises(ProviderError):
        AccessService(places, Mock()).resolve_point(station)


def test_local_train_with_subway_pois_survives_access():
    service, trip, places, _, _, _ = setup()
    original = places.search_places.side_effect
    def search(query, **kwargs):
        rows = original(query, **kwargs)
        if query == "서울역":
            rows += [dict(rows[0], id="subway", place_name="서울역 1호선", category_name="교통 > 지하철역")]
        return rows
    places.search_places.side_effect = search
    result = service.search(trip)
    assert any(c.departure_place == "서울" and c.transport_type == TransportType.TRAIN for c in result.candidates)
    assert all(c.departure_time.hour == 10 for c in result.candidates)  # 09:30 misses access + buffer.


def test_all_three_modes_have_independent_scan_budgets():
    service, trip, places, _, train, express = setup(local_origin_hub_scan_limit=1,local_origin_hub_limit=1)
    service.today = lambda: trip.start_date
    intercity = service.providers[TransportType.INTERCITY_BUS]
    original = places.search_places.side_effect
    def search(query, **kwargs):
        if query in ("고속버스터미널", "시외버스터미널"):
            return [{"id":query,"place_name":query,"x":126.91,"y":37.5,"address_name":"서울특별시"}]
        return original(query, **kwargs)
    places.search_places.side_effect = search
    for provider, name in ((express,"고속"),(intercity,"시외")):
        provider.list_terminals.side_effect = lambda query, name=name: [{"terminalId":name if query=="서울" else "dest", "terminalNm":name if query=="서울" else "구미"}]
        provider.search_trips.return_value = []
    result = service.search(trip)
    assert all(status.departure_match and status.departure_match.hubs for status in result.statuses)
    assert train.search_trips.call_count == 1
    assert express.search_trips.call_count == 1
    assert intercity.search_trips.call_count == 1


def test_arrival_order_has_no_mode_weight():
    _, trip, _, _, _, _ = setup()
    candidates=[]
    for kind, arrival in ((TransportType.INTERCITY_BUS,"1400"),(TransportType.TRAIN,"1327"),(TransportType.EXPRESS_BUS,"1325")):
        candidates.append(convert_candidate({"depplacename":"서울","arrplacename":"구미","depPlaceNm":"서울","arrPlaceNm":"구미","depplandtime":"202610011023","arrplandtime":"20261001"+arrival},kind))
    assert [c.transport_type for c in rank_candidates(candidates,trip)] == [TransportType.EXPRESS_BUS,TransportType.TRAIN,TransportType.INTERCITY_BUS]


def test_unresolved_hubs_have_diagnostics(caplog):
    service, trip, places, _, _, _ = setup()
    original=places.search_places.side_effect
    places.search_places.side_effect=lambda query,**kw: original(query,**kw) if query==trip.departure else []
    with caplog.at_level("INFO",logger="travel_ai.transport"):
        result=service.search(trip)
    assert not result.candidates
    assert "hub_unknown=" in caplog.text
    assert "좌표 미확인" in result.statuses[0].departure_match.detail
