from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from models.transport import TransportResult
from providers.http_client import ProviderError
from test_origin_resolver import resolver, row
from test_local_origin import setup


def test_building_whitespace_retry_is_bounded():
    service, provider = resolver([])
    provider.search_places.side_effect = [[], [row("검증주거 8차", category="아파트")]]
    point = service.resolve("검증주거8차")
    assert point.original_query == "검증주거8차"
    assert [c.args[0] for c in provider.search_places.call_args_list] == ["검증주거8차", "검증주거 8차"]
    provider.search_addresses.assert_not_called()


@pytest.mark.parametrize("rows", [[], [row("다른건물"), row("무관한상가", id="2")]])
def test_missing_or_unrelated_building_requests_address(rows):
    service, provider = resolver(rows)
    with pytest.raises(ProviderError, match="NEED_ADDRESS"):
        service.resolve("검증 주거8차")
    assert provider.search_places.call_count <= 3
    provider.search_addresses.assert_not_called()


def test_supplied_address_without_place_id_reuses_transport_flow():
    service, trip, places, _, train, _ = setup("검증주거8차")
    address = "서울 서초구 서초대로 1"
    places.search_addresses.return_value = [{"address_type": "ROAD_ADDR", "address_name": address, "x": 127.03, "y": 37.48}]
    result = service.search(trip, origin_address=address)
    assert result.origin_resolution_status == "RESOLVED"
    assert result.origin_status == "LOCAL_ORIGIN" and result.candidates
    point = result.candidates[0].access.access_leg.origin_point
    assert point.source == "KAKAO_ADDRESS" and point.original_query == trip.departure
    assert point.name == point.address == address
    places.search_addresses.assert_called_once_with(address)
    assert all(c.args[0] != trip.departure for c in places.search_places.call_args_list)
    assert train.search_trips.called


def start_app(monkeypatch, search):
    monkeypatch.setattr("services.transport_service.search_transport", search)
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "false")
    app = AppTest.from_file("app.py").run()
    assert not any(w.key == "origin_address_input" for w in app.text_input)
    app.text_input[0].set_value("검증주거8차")
    app.text_input[1].set_value("구미")
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    return app


def test_address_ui_retry_success_and_no_duplicate_calls(monkeypatch):
    service, trip, *_ = setup()
    resolved = service.search(trip)
    search = Mock(side_effect=[TransportResult(origin_status="NEED_ADDRESS"),
                              TransportResult(origin_status="INVALID_ORIGIN"), resolved])
    app = start_app(monkeypatch, search)
    app.button(key="submit_origin_address").click().run()
    assert search.call_count == 1
    app.text_input(key="origin_address_input").set_value("잘못된 주소")
    app.button(key="submit_origin_address").click().run()
    assert app.text_input(key="origin_address_input") and app.error
    app.text_input(key="origin_address_input").set_value("서울 서초구 서초대로 1")
    app.button(key="submit_origin_address").click().run()
    assert not app.exception
    assert search.call_count == 3
    assert search.call_args.kwargs == {"origin_address": "서울 서초구 서초대로 1"}
    assert search.call_args.args[0].departure == "검증주거8차"
    assert not any(w.key == "origin_address_input" for w in app.text_input)
    app.run()
    assert search.call_count == 3
    app.button(key="select_transport_1").click().run()
    assert not app.exception and app.session_state.selected_transport == resolved.candidates[0]
    search.side_effect = None
    search.return_value = TransportResult(origin_status="NEED_ADDRESS")
    app.button[0].click().run()
    assert not app.exception
    assert app.text_input(key="origin_address_input").value == ""
    assert app.session_state.selected_transport is None


@pytest.mark.parametrize("status", ["LOCAL_ORIGIN", "DIRECT_HUB", "AMBIGUOUS_ORIGIN", "INVALID_ORIGIN"])
def test_address_ui_only_for_need_address(monkeypatch, status):
    app = start_app(monkeypatch, Mock(return_value=TransportResult(origin_status=status)))
    assert not app.exception
    assert not any(w.key == "origin_address_input" for w in app.text_input)
