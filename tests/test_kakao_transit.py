from unittest.mock import Mock

import pytest

from models.access import AccessPoint
from providers.http_client import ProviderError
from providers.kakao_transit_provider import KakaoTransitProvider, clear_route_cache


@pytest.fixture(autouse=True)
def _clear_transit_cache():
    clear_route_cache()
    yield
    clear_route_cache()


def route(seconds=1680):
    return {"properties": {"totalTime": seconds, "totalDistance": 10000, "transfers": 1},
            "steps": [{"properties": {"type": "SUBWAY", "time": seconds - 180, "distance": 9500}},
                      {"properties": {"type": "WALKING", "time": 180, "distance": 500}}]}


def points():
    return (AccessPoint(id="1", name="출발", x=126.97, y=37.55),
            AccessPoint(id="2", name="거점", x=127.0, y=37.5))


def test_fastest_route_seconds_steps_and_contract():
    http = Mock()
    http.request_json.return_value = {"status": "OK", "routes": [route(2100), route(1680)]}
    result = KakaoTransitProvider("test-key", http).fastest_route(*points())
    assert result.duration_seconds == 1680
    assert result.transfers == 1
    assert result.steps[1].mode == "WALKING"
    args = http.request_json.call_args
    assert args.args == ("GET", "https://dapi.kakao.com/v2/routing/publictraffic")
    assert args.kwargs["params"] == {"start_x": 126.97, "start_y": 37.55, "end_x": 127.0, "end_y": 37.5}
    assert args.kwargs["headers"] == {"Authorization": "KakaoAK test-key"}


@pytest.mark.parametrize("payload", [
    {"status": "NO_RESULTS"}, {"status": "INVALID_REQUEST"}, {"status": "EQUAL_POINTS"},
    {"status": "OK", "routes": []}, {"status": "OK", "routes": [None]},
    {"status": "OK", "routes": [route(0)]}, {"status": "OK", "routes": [route(-1)]},
    {"status": "OK", "routes": [route(float("nan"))]},
    {"status": "OK", "routes": [route(float("inf"))]},
])
def test_unusable_response_is_unknown(payload):
    http = Mock()
    http.request_json.return_value = payload
    with pytest.raises(ProviderError, match="ACCESS_TIME_UNKNOWN"):
        KakaoTransitProvider("test", http).fastest_route(*points())


def test_valid_alternative_survives_malformed_route():
    http = Mock()
    http.request_json.return_value = {"status": "OK", "routes": [{}, route()]}
    assert KakaoTransitProvider("test", http).fastest_route(*points()).duration_seconds == 1680


def test_missing_key_does_not_call_network():
    http = Mock()
    with pytest.raises(ProviderError, match="missing_key"):
        KakaoTransitProvider("", http).fastest_route(*points())
    http.request_json.assert_not_called()


def test_process_route_cache_reuses_identical_od(monkeypatch):
    from providers import kakao_transit_provider as module
    module.clear_route_cache()
    http = Mock()
    http.request_json.return_value = {"status": "OK", "routes": [route()]}
    provider = KakaoTransitProvider("test-key-cache", http)
    a, b = points()
    first = provider.fastest_route(a, b)
    second = provider.fastest_route(a, b)
    other = KakaoTransitProvider("test-key-cache", http).fastest_route(a, b)
    assert first.duration_seconds == second.duration_seconds == other.duration_seconds
    assert http.request_json.call_count == 1
    assert module.route_cache_size() >= 1
    module.clear_route_cache()


def test_process_route_cache_stores_quota_failure():
    from providers import kakao_transit_provider as module
    module.clear_route_cache()
    http = Mock()
    http.request_json.side_effect = ProviderError("http_400_quota")
    provider = KakaoTransitProvider("test-key-quota", http)
    a, b = points()
    with pytest.raises(ProviderError, match="http_400_quota"):
        provider.fastest_route(a, b)
    with pytest.raises(ProviderError, match="http_400_quota"):
        KakaoTransitProvider("test-key-quota", http).fastest_route(a, b)
    assert http.request_json.call_count == 1
    module.clear_route_cache()
