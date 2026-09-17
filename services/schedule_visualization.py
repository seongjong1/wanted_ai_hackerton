"""Streamlit rendering for Phase 5 day summary / map / timeline."""
from __future__ import annotations

import streamlit as st

from models.schedule import TripSchedule
from models.transport import TransportCandidate
from models.trip_request import TripRequest
from models.place import PlaceCandidate
from services.schedule_day_view import DayView, build_schedule_views, lodging_ui_label
from services.schedule_map import build_day_deck


def format_minutes(value: float) -> str:
    return f"{value:g}분" if float(value).is_integer() else f"약 {int(value + 0.5)}분"


def render_day_summary(view: DayView) -> None:
    st.markdown(f"**{view.date:%m/%d} · {view.day_index}일차**")
    for line in view.summary.lines:
        st.write(line)


def render_day_map(view: DayView, *, key: str) -> None:
    st.markdown("**지도**")
    try:
        deck = build_day_deck(view)
        if deck is None:
            st.caption("이 날짜에 표시할 지도 좌표가 없습니다.")
            return
        st.pydeck_chart(deck, use_container_width=True, key=key)
        if view.order_line and len(view.order_line) >= 2:
            st.caption("점선/연결선은 방문 순서 안내선이며 실제 버스·도보 경로가 아닙니다.")
        if view.missing_coord_names:
            st.caption("일부 장소는 지도 위치 정보를 표시하지 못했습니다.")
        legend = (
            "● 활동  ·  ★/주황 MAIN  ·  숙=숙소  ·  임=임시 기준점  ·  도=도착 거점"
        )
        st.caption(legend)
    except Exception:
        st.caption("지도를 표시할 수 없습니다.")


def render_day_timeline(view: DayView) -> None:
    st.markdown("**일정 타임라인**")
    for event in view.timeline:
        time_label = (
            f"{event.start:%H:%M}" if event.end is None
            else f"{event.start:%H:%M} ~ {event.end:%H:%M}")
        with st.container(border=True):
            if event.kind == "FREE_TIME":
                st.caption(time_label)
                st.write(event.title)
                if event.detail:
                    st.caption(event.detail)
                continue
            if event.kind == "TRAVEL" or event.kind.startswith("RETURN") or event.kind == "OUTBOUND":
                st.caption(f"{time_label} · 이동")
            elif event.kind == "ACTIVITY":
                seq = f"{event.sequence}. " if event.sequence is not None else ""
                st.caption(time_label)
                st.write(f"**{seq}{event.title}**")
                if event.detail:
                    st.write(event.detail)
                if event.caption:
                    st.caption(event.caption)
                continue
            else:
                st.caption(time_label)
            st.write(event.title)
            if event.detail:
                st.caption(event.detail)
    if view.return_summary and view.return_summary.goal is not None:
        st.markdown("**귀가 요약**")
        rs = view.return_summary
        if rs.cutoff is not None:
            st.caption(f"목적지 활동 종료 권장 한도: {rs.cutoff:%H:%M}")
        if rs.expected is not None and rs.margin_minutes is not None:
            st.success(
                f"귀가 완료 목표 {rs.goal:%H:%M} · 예상 귀가 {rs.expected:%H:%M} · "
                f"남은 여유 {format_minutes(rs.margin_minutes)}")
        else:
            st.caption(f"귀가 완료 목표 {rs.goal:%H:%M}")


def render_schedule_visualization(
        schedule: TripSchedule, *,
        trip: TripRequest,
        selected: TransportCandidate,
        candidates: list[PlaceCandidate],
        render_raw_items,
        render_return,
) -> None:
    """Summary + Map + Timeline per day; raw detail moved to expander."""
    views = build_schedule_views(
        schedule, trip=trip, selected=selected, candidates=candidates)
    if schedule.accommodation_status == "PROVISIONAL" and schedule.accommodation:
        st.caption(
            f"숙소: 미정 · 임시 기준점: {schedule.accommodation.name}")
    elif schedule.accommodation_status == "CONFIRMED" and schedule.accommodation:
        st.caption(f"숙소: {schedule.accommodation.name}")

    if len(views) == 1 and views[0].role == "SINGLE":
        view = views[0]
        render_day_summary(view)
        render_day_map(view, key="day_map_single")
        render_day_timeline(view)
        with st.expander("일정 상세 보기", expanded=False):
            render_raw_items(schedule.items, schedule.trip_start_datetime)
            render_return(schedule)
        return

    tabs = st.tabs([f"{view.date:%m/%d} · {view.day_index}일차" for view in views])
    for tab, view, day in zip(tabs, views, schedule.days):
        with tab:
            render_day_summary(view)
            render_day_map(view, key=f"day_map_{view.day_index}")
            render_day_timeline(view)
            if view.role == "FINAL":
                render_return(schedule)
            with st.expander("일정 상세 보기", expanded=False):
                render_raw_items(day.items, day.activity_start)
                if day.role != "FINAL" and schedule.accommodation:
                    label = lodging_ui_label(schedule.accommodation_status)
                    st.caption(f"{label} · {schedule.accommodation.name}")
