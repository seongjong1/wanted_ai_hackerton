from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from test_places import trip, selected, anchor, row, service
from models.transport import TransportResult
from models.trip_request import Preference
from providers.http_client import ProviderError
from services.destination_resolver import clear_destination_cache
from services.place_service import PlaceService, belongs_to_destination


@pytest.fixture(autouse=True)
def _clear_destination_cache():
    clear_destination_cache()
    yield
    clear_destination_cache()


@pytest.mark.parametrize("radius", ["100m", "500m 이상"])
def test_main_scope_not_limited_by_activity_radius(trip, selected, anchor, radius):
    s, kakao, _ = service(anchor, [row("far", 36.2, category_name="여행 > 관광,명소")])
    result = s.search_main(trip.model_copy(update={"activity_radius": radius}), selected)
    assert len(result.candidates) == 1
    assert result.candidates[0].distance_meters > 2000
    assert result.candidates[0].role == "MAIN_DESTINATION"
    assert result.destination_scope == "구미" and result.radius_meters == 0
    assert all("radius" not in call.kwargs and "x" not in call.kwargs for call in kakao.search_places.call_args_list)
    queries = [call.args[0] for call in kakao.search_places.call_args_list]
    # Address API resolves 구미; keyword preference queries only.
    assert all(q.startswith("구미 ") for q in queries)


def test_dedup_query_evidence_scoring_and_region(trip, selected, anchor):
    s, kakao, _ = service(anchor, [])
    def search(query, **kw):
        rows = [row("repeated", category_name="여행 > 관광,명소 > 문화유적"),
                row("outside", address_name="경북 경주시")]
        if query.endswith("관광"):
            rows += [row("weak", category_name="기타")]
        return rows + rows
    kakao.search_places.side_effect = search
    result = s.search_main(trip.model_copy(update={"preferences": (Preference.SIGHTSEEING,)}), selected)
    assert [c.place_id for c in result.candidates] == ["repeated", "weak"]
    assert len(result.candidates[0].matched_queries) == 7
    assert result.candidates[0].preference_score > result.candidates[1].preference_score
    assert result.candidates[0].preference_score < 100


@pytest.mark.parametrize("destination,address,expected", [
    ("구미", "경상북도 구미시 원평동", True), ("경북 구미", "경상북도 구미시", True),
    ("부산", "부산광역시 해운대구", True), ("구미", "경기 성남시 분당구 구미동", False),
    ("구미", "경북 경주시", False), ("구미", "", False)])
def test_destination_address_validation(destination, address, expected):
    assert belongs_to_destination(destination, address) is expected


def test_main_and_nearby_roles_and_limits(trip, selected, anchor):
    s, _, _ = service(anchor, [row("near"), row("far", 36.2)])
    main = s.search_main(trip, selected)
    nearby = s.search(trip, selected)
    assert {c.place_id for c in main.candidates} == {"near", "far"}
    assert [c.place_id for c in nearby.candidates] == ["near"]
    assert all(c.role == "NEARBY_PLACE" for c in nearby.candidates)
    assert nearby.radius_meters == 500


def test_main_continues_without_hub_and_partial_failure(trip, selected):
    kakao = Mock()
    def query(text, **kwargs):
        if text.endswith("관광"):
            raise ProviderError("timeout")
        return [row()]
    kakao.search_places.side_effect = query
    kakao.search_addresses.return_value = [
        {"address_name": "경북 구미시", "x": "128.33", "y": "36.12",
         "address": {"address_name": "경북 구미시", "x": "128.33", "y": "36.12"}}]
    s = PlaceService(kakao, resolve_point=Mock(side_effect=ProviderError("no_data")))
    result = s.search_main(trip, selected)
    assert result.anchor is None and result.candidates
    assert all(c.distance_meters is None for c in result.candidates)
    assert result.status == "일부 결과"


@pytest.mark.parametrize("conditions", [False, True])
def test_main_ui_conditions_and_additional_search(monkeypatch, trip, selected, anchor, conditions):
    s, _, _ = service(anchor, [row()])
    main = s.search_main(trip, selected)
    nearby = s.search(trip, selected)
    search = Mock(side_effect=[main, nearby])
    monkeypatch.setattr("services.transport_service.search_transport", Mock(return_value=TransportResult(candidates=[selected])))
    monkeypatch.setattr("services.place_service.search_places_for_trip", search)
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "false")
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value("서울역")
    app.text_input[1].set_value("구미")
    app.multiselect[0].set_value(["관광"])
    if conditions:
        app.checkbox[0].set_value(True)  # 반려동물 동반
        app.checkbox[1].set_value(True)  # 아이 동반
        app.text_input[2].set_value("땅콩")
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.button(key="search_nearby_places").click().run()
    assert not app.exception
    assert "구미 주요 추천 장소" in [item.value for item in app.subheader]
    captions = " ".join(item.value for item in app.caption)
    for text in ("반려동물: 미확인", "아이 동반: 미확인", "알레르기: 미확인"):
        assert (text in captions) is conditions
    assert "접근 경로 구간" not in [item.label for item in app.expander]
    app.button(key="search_additional_places").click().run()
    assert not app.exception
    assert search.call_args.kwargs["nearby"] is True
    assert search.call_args.kwargs["point"].id == main.candidates[0].place_id
    assert app.session_state.place_result == main
    assert app.session_state.nearby_result == nearby


def test_display_rounding_keeps_precision():
    from app import format_minutes
    assert format_minutes(33.6667) == "약 34분"
    assert format_minutes(36.3333) == "약 36분"
    assert format_minutes(132.5333) == "약 133분"
    assert format_minutes(15) == "15분"
