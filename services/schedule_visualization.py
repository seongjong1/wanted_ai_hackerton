"""Streamlit rendering for Phase 5 day summary / map / timeline."""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from models.schedule import TripSchedule
from models.transport import TransportCandidate
from models.trip_request import TripRequest
from models.place import PlaceCandidate
from services.schedule_day_view import DayView, build_schedule_views, lodging_ui_label
from services.schedule_map import build_day_deck


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
            "숫자=방문 순서(Timeline과 동일)  ·  N MAIN=선택 MAIN  ·  "
            "H=도착 거점  ·  S=숙소  ·  P=임시 기준점  ·  R=귀가 거점  ·  선=방문 순서 안내선"
        )
        st.caption(legend)
    except Exception:
        st.caption("지도를 표시할 수 없습니다.")


def render_day_timeline(
        view: DayView,
        *,
        completed_keys: frozenset[str] | None = None,
        current_datetime: datetime | None = None,
) -> None:
    from services.route_detail_display import meaningful_route_steps, route_step_lines

    st.markdown("**일정 타임라인**")
    done = completed_keys or frozenset()
    divider_drawn = False
    for event in view.timeline:
        # Optional "current time" divider for activity events
        if (current_datetime is not None and not divider_drawn
                and event.start is not None and event.start >= current_datetime
                and event.kind in {"ACTIVITY", "FREE_TIME", "TRAVEL"}):
            st.caption(f"──────── 설정한 현재 시각 {current_datetime:%H:%M} ────────")
            divider_drawn = True
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
                is_done = False
                if event.place_id and event.start is not None:
                    is_done = f"{event.place_id}|{event.start.isoformat()}" in done
                prefix = "✓ 완료 · " if is_done else ""
                st.write(f"**{prefix}{seq}{event.title}**")
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
            steps = meaningful_route_steps(getattr(event, "route_steps", ()) or ())
            show_detail = bool(steps) and event.kind in {
                "TRAVEL", "RETURN_HUB", "RETURN_ACCESS"}
            if event.kind == "OUTBOUND" and steps and "출발지 접근" in (event.detail or ""):
                show_detail = True
            if show_detail:
                label = (
                    "접근 경로 자세히 보기"
                    if event.kind in {"OUTBOUND", "RETURN_ACCESS"}
                    else "이동 경로 자세히 보기")
                with st.expander(label, expanded=False):
                    for step in steps:
                        for line in route_step_lines(step):
                            st.text(line)
    if view.return_summary is not None and view.return_summary.cutoff is not None:
        st.markdown("**귀가 요약**")
        st.caption(f"목적지 활동 종료 권장 한도: {view.return_summary.cutoff:%H:%M}")


def render_schedule_visualization(
        schedule: TripSchedule, *,
        trip: TripRequest,
        selected: TransportCandidate,
        candidates: list[PlaceCandidate],
        render_raw_items,
        render_return,
        completed_keys: frozenset[str] | None = None,
        current_datetime: datetime | None = None,
) -> None:
    """Summary + Map + Timeline per day; raw detail / return moved to expander."""
    views = build_schedule_views(
        schedule, trip=trip, selected=selected, candidates=candidates)
    if schedule.accommodation_status == "PROVISIONAL" and schedule.accommodation:
        st.caption(
            f"숙소 미정 · 임시 기준점: {schedule.accommodation.name}")
    elif schedule.accommodation_status == "CONFIRMED" and schedule.accommodation:
        st.caption(f"숙소: {schedule.accommodation.name}")

    def _detail_expander(view: DayView, day_items, day_start, *, show_return: bool) -> None:
        with st.expander("일정 상세 보기", expanded=False):
            render_raw_items(day_items, day_start)
            if day_items is not schedule.items and schedule.accommodation and view.role != "FINAL":
                label = lodging_ui_label(schedule.accommodation_status)
                st.caption(f"{label} · {schedule.accommodation.name}")
        if show_return:
            with st.expander("돌아가는 길 자세히 보기", expanded=False):
                render_return(schedule)

    if len(views) == 1 and views[0].role == "SINGLE":
        view = views[0]
        render_day_summary(view)
        render_day_map(view, key="day_map_single")
        render_day_timeline(
            view, completed_keys=completed_keys, current_datetime=current_datetime)
        _detail_expander(view, schedule.items, schedule.trip_start_datetime, show_return=True)
        return

    tabs = st.tabs([f"{view.date:%m/%d} · {view.day_index}일차" for view in views])
    for tab, view, day in zip(tabs, views, schedule.days):
        with tab:
            render_day_summary(view)
            render_day_map(view, key=f"day_map_{view.day_index}")
            render_day_timeline(
                view, completed_keys=completed_keys, current_datetime=current_datetime)
            _detail_expander(
                view, day.items, day.activity_start, show_return=(view.role == "FINAL"))
