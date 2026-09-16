from unittest.mock import Mock

from providers.http_client import ProviderError
from services.location_service import HubResolver, LocationContext, resolve_context


def test_kakao_province_and_city():
    kakao = Mock()
    kakao.search_places.return_value = [{"address_name": "경북 구미시 원평동", "place_name": "구미역"}]
    assert resolve_context("경북 구미", kakao) == LocationContext("경북 구미", "경북", "구미")


def test_ambiguous_region_does_not_guess():
    kakao = Mock()
    kakao.search_places.return_value = [{"address_name": "경기 광주시 경안동"}, {"address_name": "광주 서구 광천동"}]
    context = resolve_context("광주", kakao)
    assert context.ambiguous
    provider = Mock()
    assert not HubResolver().bus(context, provider).hubs
    provider.list_terminals.assert_not_called()


def test_kakao_failure_falls_back_to_exact_catalog_city():
    kakao = Mock()
    kakao.search_places.side_effect = ProviderError("timeout")
    context = resolve_context("서울역", kakao)
    train = Mock()
    train.list_cities.return_value = [{"citycode": "11", "cityname": "서울특별시"}]
    train.list_stations.return_value = [{"nodeid": "actual", "nodename": "서울"}]
    assert HubResolver().train(context, train).hubs[0].id == "actual"


def test_gumi_station_via_province():
    train = Mock()
    train.list_cities.return_value = [{"citycode": "37", "cityname": "경상북도"}]
    train.list_stations.return_value = [{"nodeid": "actual", "nodename": "구미"},
                                      {"nodeid": "other", "nodename": "김천구미"}]
    match = HubResolver().train(LocationContext("경북 구미", "경북", "구미"), train)
    assert [h.name for h in match.hubs] == ["구미"]


def test_unknown_specific_station_never_falls_back_to_city():
    train = Mock()
    train.list_cities.return_value = [{"citycode": "11", "cityname": "서울특별시"}]
    train.list_stations.return_value = [{"nodeid": "actual", "nodename": "서울"}]
    assert not HubResolver().train(LocationContext("없는역", "서울", "서울"), train).hubs


def test_district_uses_confirmed_city_terminals():
    bus = Mock()
    bus.list_terminals.return_value = [{"terminalId": "real", "terminalNm": "서울경부"}]
    match = HubResolver().bus(LocationContext("강남", "서울", "서울"), bus)
    bus.list_terminals.assert_called_once_with("서울")
    assert match.hubs[0].name == "서울경부"


def test_multiple_hubs_are_capped_and_disclosed():
    bus = Mock()
    bus.list_terminals.return_value = [{"terminalId": str(i), "terminalNm": f"서울{i}"} for i in range(5)]
    match = HubResolver().bus(LocationContext("서울"), bus)
    assert len(match.hubs) == 3 and match.limited
