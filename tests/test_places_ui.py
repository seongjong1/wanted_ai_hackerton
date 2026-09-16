from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from models.access import AccessPoint
from models.place import PlaceResult
from models.transport import TransportResult, TransportType
from models.trip_request import Preference
from services.place_service import convert_place
from services.transport_service import convert_candidate


def test_places_search_cache_and_selection_invalidation(monkeypatch):
    selected=convert_candidate({"depplacename":"서울","arrplacename":"구미","depplandtime":"20261001090000",
                                "arrplandtime":"20261001120000"},TransportType.TRAIN)
    monkeypatch.setattr("services.transport_service.search_transport",Mock(return_value=TransportResult(candidates=[selected, selected.model_copy(update={"grade": "다른 등급"})])))
    anchor=AccessPoint(id="station",name="구미역",x=128.33,y=36.12)
    candidate=convert_place({"id":"p1","place_name":"테스트 장소","x":"128.33","y":"36.121",
                             "address_name":"테스트 주소"},anchor,Preference.FOOD)
    search=Mock(return_value=PlaceResult(anchor=anchor,anchor_candidates=(anchor,),candidates=[candidate],radius_meters=500,status="정상"))
    monkeypatch.setattr("services.place_service.search_places_for_trip",search)
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS","false")
    app=AppTest.from_file("app.py").run()
    app.text_input[0].set_value("서울역")
    app.text_input[1].set_value("구미")
    app.multiselect[0].set_value(["맛집"])
    app.button[0].click().run()
    app.button(key="select_transport_1").click().run()
    search.assert_not_called()
    app.button(key="search_nearby_places").click().run()
    assert not app.exception
    assert app.session_state.place_candidates==[candidate]
    assert any("테스트 장소" in item.value for item in app.markdown)
    app.run()
    assert search.call_count==1
    app.button(key="select_transport_2").click().run()
    assert "place_candidates" not in app.session_state
