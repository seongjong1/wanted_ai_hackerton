from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from test_places import trip, selected, anchor, row, service
from models.trip_request import Preference
from models.transport import TransportResult
from services.place_service import rank_places


@pytest.mark.parametrize("main", [True, False])
def test_food_queries_dedup_cafes_and_scores(trip, selected, anchor, main):
    s, kakao, _ = service(anchor, [])
    def search(query, **kwargs):
        rows = [row("meal", category_name="음식점 > 한식 > 국밥"),
                row("cafe", category_name="음식점 > 카페 > 커피전문점")]
        if query.endswith("음식점"):
            rows += [row("generic", category_name="음식점")]
        return rows + rows
    kakao.search_places.side_effect = search
    food = trip.model_copy(update={"preferences": (Preference.FOOD,)})
    result = s.search_main(food, selected) if main else s.search(food, selected)
    assert [c.place_id for c in result.candidates] == ["meal", "generic"]
    assert result.candidates[0].preference_score > result.candidates[1].preference_score
    assert all(c.preference_score < 100 for c in result.candidates)
    assert len(result.candidates[0].matched_queries) == 3
    assert len(result.candidates[1].matched_queries) == 1
    # MAIN path: DestinationResolver address + preference queries; nearby keeps radius search only.
    assert kakao.search_places.call_count == (3 if main else 3)
    if main:
        assert kakao.search_addresses.called
    assert all(("radius" not in call.kwargs) if main else call.kwargs["radius"] == 500
               for call in kakao.search_places.call_args_list)


def test_main_distance_neutral_nearby_distance_matters(trip, selected, anchor):
    s, _, _ = service(anchor, [row("a",36.2), row("z",36.1201)])
    result = s.search_main(trip, selected)
    assert [c.place_id for c in result.candidates] == ["a", "z"]
    swapped = [c.model_copy(update={"distance_meters": 0 if c.place_id == "a" else 10000}) for c in result.candidates]
    assert [c.place_id for c in rank_places(swapped, trip)] == ["a", "z"]
    nearby = [c.model_copy(update={"role":"NEARBY_PLACE"}) for c in result.candidates]
    assert [c.place_id for c in rank_places(nearby, trip)] == ["z", "a"]
    assert nearby[0].preference_score == nearby[1].preference_score


def test_cafe_allowed_for_rest_not_food(trip, selected, anchor):
    s, _, _ = service(anchor, [row(category_name="음식점 > 카페")])
    result = s.search(trip.model_copy(update={"preferences": (Preference.FOOD, Preference.REST)}), selected)
    assert len(result.candidates) == 1
    assert result.candidates[0].matched_preferences == (Preference.REST,)
    assert result.candidates[0].matched_queries == ("카페",)


def test_ui_hides_scores_and_folds_remaining(monkeypatch, trip, selected, anchor):
    s, _, _ = service(anchor, [row(str(i)) for i in range(9)])
    result = s.search_main(trip, selected)
    monkeypatch.setattr("services.transport_service.search_transport",Mock(return_value=TransportResult(candidates=[selected])))
    monkeypatch.setattr("services.place_service.search_places_for_trip",Mock(return_value=result))
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS","false")
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value("서울역")
    app.text_input[1].set_value("구미")
    app.multiselect[0].set_value(["맛집"])
    app.selectbox[0].set_value("500m 이상")
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    app.button(key="search_nearby_places").click().run()
    assert not app.exception
    assert not any("성향 점수" in item.value for item in app.markdown)
    assert any("주변 검색 범위: 최대 2km" in item.value for item in app.caption)
    more = next(item for item in app.expander if item.label == "더 보기")
    assert len(more.markdown) == 6  # Three remaining cards: name and distance each.
    assert app.session_state.place_candidates == result.candidates


@pytest.mark.parametrize("real", [False, True])
def test_route_details_only_for_meaningful_segments(monkeypatch, selected, real):
    from datetime import datetime
    from models.access import AccessLeg, AccessStep, BoardingAssessment
    from models.transport import KST
    step = AccessStep(mode="SUBWAY" if real else " ", duration_seconds=60 if real else 0,
                      distance_meters=100 if real else 0)
    leg = AccessLeg(origin="출발",destination="서울역",transport_modes=("SUBWAY",),
                    duration_minutes=1,departure_time=datetime(2026,10,1,8,tzinfo=KST),provider="test",steps=(step,))
    selected = selected.model_copy(update={"access":BoardingAssessment(leg,15,selected.departure_time,selected.arrival_time)})
    monkeypatch.setattr("services.transport_service.search_transport",Mock(return_value=TransportResult(candidates=[selected])))
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS","false")
    app=AppTest.from_file("app.py").run()
    app.text_input[0].set_value("서울역")
    app.text_input[1].set_value("구미")
    app.multiselect[0].set_value(["맛집"])
    app.button[0].click().run()
    assert not app.exception
    assert "접근 경로 구간" not in [item.label for item in app.expander]
    assert ("접근 경로 자세히 보기" in [item.label for item in app.expander]) is real
    if real:
        assert any("지하철" in item.value and "100m" in item.value for item in app.text)
