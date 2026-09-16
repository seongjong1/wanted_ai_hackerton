import json
from datetime import date
from unittest.mock import Mock

import pytest
import requests
from pydantic import BaseModel

from config import load_settings
from providers.groq_provider import GroqProvider
from providers.http_client import HttpClient, ProviderError
from providers.kakao_provider import KakaoProvider
from providers.tago_bus_provider import TagoBusProvider
from providers.tago_train_provider import TagoTrainProvider
from services.health_service import check_connections


def response(payload=None, status=200, headers=None):
    value = requests.Response()
    value.status_code = status
    value._content = json.dumps(payload).encode()
    value._content_consumed = True
    value.headers.update(headers or {})
    return value


def client_with(*results):
    session, sleep = Mock(), Mock()
    session.request.side_effect = results
    return HttpClient(session=session, sleep=sleep), session, sleep


def envelope(items=None, code="00"):
    return {"response": {"header": {"resultCode": code},
                         "body": {"items": {"item": items if items is not None else []}}}}


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_status_retry(status):
    http, session, sleep = client_with(response(status=status), response({"ok": True}))
    assert http.request_json("GET", "https://example.test", provider="test") == {"ok": True}
    assert session.request.call_count == 2
    assert sleep.call_count == 1
    assert session.request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("status", [401, 403, 400, 404, 302])
def test_permanent_status_no_retry(status):
    http, session, sleep = client_with(response(status=status))
    with pytest.raises(ProviderError, match=f"http_{status}"):
        http.request_json("GET", "https://example.test", provider="test")
    assert session.request.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("failure", [requests.Timeout, requests.ConnectionError])
def test_network_retry_exhaustion_and_redaction(failure, caplog):
    secret = "DO_NOT_EXPOSE_TEST_KEY"
    http, session, sleep = client_with(*(failure(secret) for _ in range(3)))
    with pytest.raises(ProviderError) as exc:
        http.request_json("GET", "https://example.test", provider="test",
                          params={"serviceKey": secret})
    assert session.request.call_count == 3
    assert sleep.call_count == 2
    assert secret not in str(exc.value) + caplog.text


def test_long_retry_after_returns_without_retry():
    http, session, sleep = client_with(response(status=429, headers={"Retry-After": "60"}))
    with pytest.raises(ProviderError):
        http.request_json("GET", "https://example.test", provider="test")
    sleep.assert_not_called()


def test_invalid_json():
    invalid = response()
    invalid._content = b"<error>secret</error>"
    http, _, _ = client_with(invalid)
    with pytest.raises(ProviderError, match="invalid_json"):
        http.request_json("GET", "https://example.test", provider="test")


def test_tago_singleton_and_correct_endpoint():
    http, session, _ = client_with(response(envelope({"citycode": 11})))
    assert TagoTrainProvider("a%2Bb%3D", http).list_cities() == [{"citycode": 11}]
    assert session.request.call_args.args[1].endswith("/TrainInfo/GetCtyCodeList")
    assert session.request.call_args.kwargs["params"]["serviceKey"] == "a+b="


@pytest.mark.parametrize("kind,service", [("express", "ExpBusInfo"), ("intercity", "SuburbsBusInfo")])
def test_bus_endpoint(kind, service):
    http, session, _ = client_with(response(envelope([])))
    assert TagoBusProvider("test", http, kind).list_cities() == []
    assert session.request.call_args.args[1].endswith(f"/{service}/GetCtyCodeList")


def test_tago_body_error_retry():
    http, session, _ = client_with(response(envelope(code="05")), response(envelope([])))
    assert TagoTrainProvider("test", http).list_cities() == []
    assert session.request.call_count == 2


def test_tago_auth_error_not_success():
    http, session, _ = client_with(response(envelope(code="30")))
    with pytest.raises(ProviderError, match="tago_30"):
        TagoTrainProvider("test", http).healthcheck()
    assert session.request.call_count == 1


def test_tago_no_data_vs_malformed():
    body = {"response": {"header": {"resultCode": "00"}, "body": {"items": "", "totalCount": 0}}}
    http, _, _ = client_with(response(body), response({"response": {"header": {"resultCode": "00"}}}))
    provider = TagoTrainProvider("test", http)
    assert provider.list_cities() == []
    with pytest.raises(ProviderError, match="invalid_response"):
        provider.list_cities()


def test_kakao_search_empty_and_radius():
    http, session, _ = client_with(response({"documents": []}))
    provider = KakaoProvider("test", http)
    assert provider.search_places("서울", x=127, y=37, radius=500) == []
    assert session.request.call_args.kwargs["headers"] == {"Authorization": "KakaoAK test"}
    with pytest.raises(ValueError):
        provider.search_places("서울", radius=500)


def test_groq_health_does_not_generate():
    http, session, _ = client_with(response({"data": [{"id": "openai/gpt-oss-120b"}]}))
    GroqProvider("test", http).healthcheck()
    assert session.request.call_args.args[0] == "GET"
    assert session.request.call_args.args[1].endswith("/models")


class Selection(BaseModel):
    candidate_id: int


@pytest.mark.parametrize("content,valid", [('{"candidate_id": 1}', True), ('bad JSON', False), ('{}', False)])
def test_groq_schema_validation(content, valid):
    http, _, _ = client_with(response({"choices": [{"message": {"content": content}}]}))
    provider = GroqProvider("test", http)
    if valid:
        assert provider.generate_structured("select", {}, Selection).candidate_id == 1
    else:
        with pytest.raises(ProviderError, match="invalid_llm_output"):
            provider.generate_structured("select", {}, Selection)


def test_groq_post_timeout_not_repeated():
    http, session, _ = client_with(requests.Timeout())
    with pytest.raises(ProviderError):
        GroqProvider("test", http).generate_structured("select", {}, Selection)
    assert session.request.call_count == 1


def test_missing_secrets_graceful_no_network():
    http, session, _ = client_with()
    results = check_connections(load_settings({}, {}), http)
    assert len(results) == 5
    assert all(result.status == "미설정" for result in results)
    session.request.assert_not_called()


def test_config_precedence_and_repr():
    settings = load_settings({"GROQ_API_KEY": "secret1"}, {"GROQ_API_KEY": "secret2"})
    assert settings.groq_api_key == "secret2"
    assert "secret" not in repr(settings)


def test_one_provider_failure_does_not_block_others():
    http, _, _ = client_with(response(status=401), response(envelope({"cityCode": "11"})),
                            response(envelope({"cityCode": "11"})), response(envelope({"cityCode": "11"})),
                            response({"data": [{"id": "openai/gpt-oss-120b"}]}))
    settings = load_settings({}, {name: "test" for name in
                                   ["GROQ_API_KEY", "KAKAO_REST_API_KEY", "DATA_GO_KR_API_KEY"]})
    results = check_connections(settings, http)
    assert results[0].status == "확인 필요"
    assert all(result.status == "정상" for result in results[1:])


@pytest.mark.parametrize("kind,path,id_key", [
    ("train", "TrainInfo/GetStrtpntAlocFndTrainInfo", "depPlaceId"),
    ("express", "ExpBusInfo/GetStrtpntAlocFndExpbusInfo", "depTerminalId"),
    ("intercity", "SuburbsBusInfo/GetStrtpntAlocFndSuberbsBusInfo", "depTerminalId"),
])
def test_transport_query_contract(kind, path, id_key):
    http, session, _ = client_with(response(envelope([])))
    provider = TagoTrainProvider("test", http) if kind == "train" else TagoBusProvider("test", http, kind)
    assert provider.search_trips("actual-dep", "actual-arr", date(2026, 10, 1)) == []
    call = session.request.call_args
    assert call.args[1].endswith(path)
    assert call.kwargs["params"][id_key] == "actual-dep"
    assert call.kwargs["params"]["depPlandTime"] == "20261001"


def test_pagination_reads_later_pages():
    first, second = envelope([{"nodeid": "first"}]), envelope([{"nodeid": "second"}])
    for payload in (first, second):
        payload["response"]["body"]["totalCount"] = 2
    http, session, _ = client_with(response(first), response(second))
    rows = TagoTrainProvider("test", http).list_stations("11")
    assert [r["nodeid"] for r in rows] == ["first", "second"]
    assert session.request.call_args.kwargs["params"]["pageNo"] == 2


def test_pagination_limit_not_silently_truncated():
    data = envelope([{"nodeid": "first"}])
    data["response"]["body"]["totalCount"] = 2
    http, _, _ = client_with(response(data))
    with pytest.raises(ProviderError, match="pagination_limit"):
        TagoTrainProvider("test", http).get_all_items("GetCtyAcctoTrainSttnList", max_pages=1)


def test_repeated_page_stops():
    data = envelope([{"nodeid": "first"}])
    data["response"]["body"]["totalCount"] = 2
    http, _, _ = client_with(response(data), response(data))
    with pytest.raises(ProviderError, match="pagination_stalled"):
        TagoTrainProvider("test", http).list_stations("11")
