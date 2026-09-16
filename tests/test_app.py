from streamlit.testing.v1 import AppTest
from models.transport import TransportResult
from unittest.mock import Mock


def test_ui_validation_and_session_state(monkeypatch):
    search = Mock(return_value=TransportResult())
    monkeypatch.setattr("services.transport_service.search_transport", search)
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "false")
    app = AppTest.from_file("app.py").run()
    assert not app.exception
    app.button[0].click().run()
    assert len(app.error) == 3
    app.text_input[0].set_value("서울역")
    app.text_input[1].set_value("부산")
    app.multiselect[0].set_value(["맛집", "관광"])
    app.button[0].click().run()
    assert not app.exception
    assert len(app.success) == 1
    assert app.session_state.trip_request.destination == "부산"
    app.run()
    assert len(app.success) == 1
    assert search.call_count == 1
    app.text_input[1].set_value("서울역")
    app.button[0].click().run()
    assert len(app.error) == 1
    assert len(app.success) == 0
    assert "transport_candidates" not in app.session_state
    assert "selected_transport" not in app.session_state


def test_diagnostics_missing_secrets(monkeypatch):
    for name in ["DATA_GO_KR_API_KEY", "KAKAO_REST_API_KEY", "GROQ_API_KEY"]:
        # A blank override isolates this test even when local runtime secrets exist.
        monkeypatch.setenv(name, " ")
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "true")
    app = AppTest.from_file("app.py").run()
    app.button[1].click().run()
    assert not app.exception
    assert len(app.session_state.health_results) == 5
    assert all(r.status == "미설정" for r in app.session_state.health_results)


def test_transport_cards_selection_and_rerun(monkeypatch):
    from services.transport_service import convert_candidate
    from models.transport import TransportType, TransportStatus
    candidate = convert_candidate({"depplacename": "서울", "arrplacename": "부산",
                                   "depplandtime": "20261001093000", "arrplandtime": "20261001120000",
                                   "traingradename": "KTX", "trainno": "101", "adultcharge": "50000"},
                                  TransportType.TRAIN)
    result = TransportResult([candidate], {TransportType.TRAIN: [candidate]},
                             [TransportStatus(TransportType.TRAIN, "정상", "조회 성공"),
                              TransportStatus(TransportType.EXPRESS_BUS, "조회 실패", "응답 시간 초과")])
    search = Mock(return_value=result)
    monkeypatch.setattr("services.transport_service.search_transport", search)
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "false")
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value("서울")
    app.text_input[1].set_value("부산")
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    assert not app.exception
    assert len(app.session_state.transport_candidates) == 1
    app.button(key="select_transport_1").click().run()
    assert not app.exception
    assert app.session_state.selected_transport == candidate
    assert search.call_count == 1
    assert not any("고속버스 · 조회 실패" in item.value for item in app.markdown)
    assert app.session_state.transport_result.statuses == result.statuses
    assert app.session_state.transport_result.by_type == result.by_type
    assert "선택한 교통편" in [item.value for item in app.subheader]
    assert "주변 추천 장소" in [item.value for item in app.subheader]
    app.button[0].click().run()
    assert app.session_state.selected_transport is None
    assert search.call_count == 2


def test_access_breakdown_render_and_selection(monkeypatch):
    from datetime import datetime
    from models.access import AccessLeg, BoardingAssessment
    from models.transport import KST, TransportType
    from services.transport_service import convert_candidate
    candidate = convert_candidate({"depPlaceNm": "서울경부", "arrPlaceNm": "부산",
                                   "depPlandTime": "202610011000", "arrPlandTime": "202610011300",
                                   "charge": "30000"}, TransportType.EXPRESS_BUS)
    leg = AccessLeg(origin="서울역", destination="서울경부", transport_modes=("SUBWAY",),
                    duration_minutes=28, departure_time=datetime(2026,10,1,9,tzinfo=KST),provider="test")
    candidate = candidate.model_copy(update={"access": BoardingAssessment(leg,15,candidate.departure_time,candidate.arrival_time)})
    result = TransportResult(candidates=[candidate],access_checked=True)
    monkeypatch.setattr("services.transport_service.search_transport",Mock(return_value=result))
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "false")
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value("서울역")
    app.text_input[1].set_value("부산")
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    assert not app.exception
    assert any("총 소요시간 240분" in item.value for item in app.markdown)
    assert any("09:43:00" in item.value for item in app.markdown)
    app.button(key="select_transport_1").click().run()
    assert app.session_state.selected_transport.access.is_feasible
