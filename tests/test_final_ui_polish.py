"""Final UI / UX polish — branding, section order, collapse labels (no business-logic changes)."""
from __future__ import annotations

from pathlib import Path


def test_case1_2_3_branding_and_no_phase_in_ui():
    src = Path("app.py").read_text(encoding="utf-8")
    assert 'st.title("여행 다시짜기")' in src
    assert 'page_title="여행 다시짜기"' in src
    assert 'st.title("AI 여행 플래너")' not in src
    assert 'page_title="AI 여행 플래너"' not in src
    assert 'st.caption("Phase 5' not in src
    assert "Phase 6" not in src or "Phase 6.3" in Path("services/replan_ui.py").read_text(encoding="utf-8")
    # User-facing Phase labels must not appear in app.py captions/titles
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("st.") and "Phase" in stripped and "Phase 6.3" not in stripped:
            assert "caption" not in stripped and "title" not in stripped and "subheader" not in stripped
    assert "실제 대중교통으로 출발부터 귀가까지" in src
    assert "남은 일정만 다시 계산" in src
    assert "실제 교통 기반" in src
    assert "동선 기반 일정" in src
    assert "여행 중 재계획" in src


def test_case4_conditions_expander():
    src = Path("app.py").read_text(encoding="utf-8")
    assert 'expander("여행 조건 더 설정하기"' in src
    assert 'subheader("여행 계획 만들기")' in src
    assert 'form_submit_button("여행 계획 만들기"' in src


def test_case5_6_transport_collapse_labels():
    src = Path("app.py").read_text(encoding="utf-8")
    assert 'subheader("선택한 교통편")' in src
    assert "다른 교통편 보기" in src
    assert "접근 경로 자세히 보기" in src
    assert "compact=True" in src


def test_case7_8_accommodation_collapse_labels():
    src = Path("app.py").read_text(encoding="utf-8")
    assert "다른 숙소 보기" in src
    assert "선택한 숙소" in src
    assert "숙소는 정하셨나요?" in src
    assert "아직 숙소를 정하지 않았어요" in src


def test_case9_10_main_collapse():
    src = Path("app.py").read_text(encoding="utf-8")
    assert "선택한 기준 장소" in src
    assert "expand_main_place_list" in src
    assert "main_place_list_expanded" in src


def test_case11_12_arrival_and_zero_access():
    day = Path("services/schedule_day_view.py").read_text(encoding="utf-8")
    assert "merge_destination_hub_arrivals" in day
    assert "is_trivial_same_place_access" in day
    assert "바로 출발" in day or "접근 이동 없음" in day
    assert "dur <= 0.5" in day


def test_case13_no_green_return_success_card():
    src = Path("app.py").read_text(encoding="utf-8")
    assert "st.success(f\"귀가 완료 목표" not in src
    assert "돌아가는 길 자세히 보기" in Path("services/schedule_visualization.py").read_text(encoding="utf-8")


def test_case14_15_replan_section():
    src = Path("app.py").read_text(encoding="utf-8")
    assert 'subheader("일정이 틀어졌나요?")' in src
    assert "지금 구미역이야" not in src
    assert "replan_chip_" in src
    # Replan is invoked from schedule results after visualization
    assert "render_schedule_visualization(" in src
    assert "render_replan_panel(" in src
    vis_idx = src.index("render_schedule_visualization(")
    replan_idx = src.index("render_replan_panel(")
    assert vis_idx < replan_idx


def test_case_section_order_transport_before_accommodation():
    src = Path("app.py").read_text(encoding="utf-8")
    # After selected transport: accommodation then places
    block_start = src.index("if selected:")
    block = src[block_start:block_start + 800]
    assert "render_accommodation_panel()" in block
    assert "render_place_results()" in block
    assert block.index("render_accommodation_panel()") < block.index("render_place_results()")


def test_no_generate_schedule_cta_in_results():
    src = Path("app.py").read_text(encoding="utf-8")
    assert 'st.button("여행 일정 생성"' not in src
    assert 'key="generate_schedule"' not in src
    assert "schedule_anchor_pending" in src
    assert "일정을 생성할 수 없습니다. 이전 단계에서 여행 조건을 다시 확인해주세요." in src
