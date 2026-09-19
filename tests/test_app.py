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
    assert any("여행 조건을 확인했습니다" in c.value for c in app.caption)
    assert app.session_state.trip_request.destination == "부산"
    app.run()
    assert any("여행 조건을 확인했습니다" in c.value for c in app.caption)
    assert search.call_count == 1
    app.text_input[1].set_value("서울역")
    app.button[0].click().run()
    assert len(app.error) == 1
    assert not any("여행 조건을 확인했습니다" in c.value for c in app.caption)
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


def _render_access_app(monkeypatch, leg):
    from datetime import datetime
    from models.access import BoardingAssessment
    from models.transport import KST, TransportType
    from services.transport_service import convert_candidate
    candidate = convert_candidate({"depPlaceNm": "서울경부", "arrPlaceNm": "부산",
                                   "depPlandTime": "202610011000", "arrPlandTime": "202610011300",
                                   "charge": "30000"}, TransportType.EXPRESS_BUS)
    candidate = candidate.model_copy(
        update={"access": BoardingAssessment(leg, 15, candidate.departure_time, candidate.arrival_time)})
    monkeypatch.setattr("services.transport_service.search_transport",
                        Mock(return_value=TransportResult(candidates=[candidate], access_checked=True)))
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "false")
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value("서울역")
    app.text_input[1].set_value("부산")
    app.multiselect[0].set_value(["관광"])
    return app.button[0].click().run(), candidate


def test_render_candidate_estimated_field_does_not_attribute_error(monkeypatch):
    from datetime import datetime
    from models.access import AccessLeg, AccessStep
    from models.transport import KST
    leg = AccessLeg(
        origin="서울역", destination="서울경부", transport_modes=("ESTIMATED",),
        duration_minutes=20, departure_time=datetime(2026, 10, 1, 9, tzinfo=KST),
        provider="ESTIMATED", estimated=True, note="직선거리 기반 추정",
        steps=(AccessStep(mode="ESTIMATED", duration_seconds=1200, distance_meters=6000,
                          guidance="직선거리 기반 추정 · 실제 대중교통 경로 아님"),),
    )
    assert hasattr(leg, "estimated") and leg.estimated is True
    app, _ = _render_access_app(monkeypatch, leg)
    assert not app.exception
    visible = "\n".join(item.value for elements in (app.markdown, app.caption) for item in elements)
    assert "접근 이동(추정)" in visible
    assert "실제 대중교통 경로가 아닙니다" in visible


def test_render_candidate_provider_estimated_without_flag_still_shows_estimate(monkeypatch):
    from datetime import datetime
    from models.access import AccessLeg
    from models.transport import KST
    leg = AccessLeg(
        origin="서울역", destination="서울경부", transport_modes=("WALKING",),
        duration_minutes=20, departure_time=datetime(2026, 10, 1, 9, tzinfo=KST),
        provider="ESTIMATED", estimated=False,
    )
    app, _ = _render_access_app(monkeypatch, leg)
    assert not app.exception
    visible = "\n".join(item.value for elements in (app.markdown, app.caption) for item in elements)
    assert "접근 이동(추정)" in visible
    assert "실제 대중교통 경로가 아닙니다" in visible


def test_render_candidate_hides_same_place_zero_access(monkeypatch):
    from datetime import datetime
    from models.access import AccessLeg, AccessPoint
    from models.transport import KST
    station = AccessPoint(id="station", name="서울역", x=126.97, y=37.55)
    leg = AccessLeg(
        origin="서울역", destination="서울역", transport_modes=("SAME_PLACE",),
        duration_minutes=0, distance_meters=0,
        departure_time=datetime(2026, 10, 1, 9, tzinfo=KST),
        provider="Kakao Local · same place ID",
        origin_point=station, destination_point=station)
    app, _ = _render_access_app(monkeypatch, leg)
    assert not app.exception
    visible = "\n".join(item.value for elements in (app.markdown, app.caption) for item in elements)
    assert "서울역에서 바로 출발" in visible
    assert "접근 이동: 서울역 → 서울역" not in visible
    assert "서울역 → 서울역 · 0분" not in visible

