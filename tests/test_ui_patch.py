from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from models.transport import TransportResult, TransportStatus, TransportType
from services.transport_service import convert_candidate


@pytest.mark.parametrize("count", [0, 1, 5, 7])
def test_clean_recommendations_preserve_internal_results(monkeypatch, count):
    candidates = [convert_candidate({
        "depplacename": "서울", "arrplacename": "구미",
        "depplandtime": "20261001093000", "arrplandtime": "20261001120000",
        "traingradename": f"테스트 등급 {i}", "trainno": str(i),
    }, TransportType.TRAIN) for i in range(count)]
    result = TransportResult(
        candidates=candidates, by_type={TransportType.TRAIN: candidates},
        statuses=[TransportStatus(TransportType.TRAIN, "일부 결과",
                                 "ACCESS_TIME_UNKNOWN 2개 제외 · TAGO 역 이름 일치")],
        access_checked=True,
    )
    search = Mock(return_value=result)
    monkeypatch.setattr("services.transport_service.search_transport", search)
    monkeypatch.setenv("ENABLE_API_DIAGNOSTICS", "false")
    app = AppTest.from_file("app.py").run()
    app.text_input[0].set_value("서울역")
    app.text_input[1].set_value("구미")
    app.multiselect[0].set_value(["관광"])
    app.button[0].click().run()
    assert not app.exception
    visible = "\n".join(item.value for elements in
                        (app.markdown, app.caption, app.subheader, app.info) for item in elements)
    visible += "\n" + "\n".join(item.label for item in app.expander)
    for removed in ("입력 조건 확인", "교통편 후보", "ACCESS_TIME_UNKNOWN",
                    "TAGO 역 이름 일치", "열차 · 일부 결과", "교통수단별 후보",
                    "조회한 직통편 중", "철도·버스 승차 버퍼 기본"):
        assert removed not in visible
    assert not app.json
    assert "추천 교통편" in [item.value for item in app.subheader]
    cards = [item.value for item in app.markdown if item.value.startswith("**추천 ")]
    assert cards == [f"**추천 {i + 1} · 열차 · 테스트 등급 {i}**"
                     for i in range(min(count, 5))]
    assert len([button for button in app.button if button.label == "이 교통편 선택"]) == min(count, 5)
    assert app.session_state.trip_request.departure == "서울역"
    assert app.session_state.transport_candidates == candidates
    assert app.session_state.transport_result.statuses == result.statuses
    assert app.session_state.transport_result.by_type == result.by_type
    if count:
        index = min(count, 5)
        app.button(key=f"select_transport_{index}").click().run()
        assert not app.exception
        assert app.session_state.selected_transport == candidates[index - 1]
        assert "주변 추천 장소" in [item.value for item in app.subheader]
        assert app.button(key="search_nearby_places")
    else:
        assert any("조건에 맞는 가는 교통편을 찾지 못했습니다" in item.value for item in app.info)
    assert search.call_count == 1
