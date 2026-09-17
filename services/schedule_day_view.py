"""Phase 5 — read-only day visualization models from an existing TripSchedule.

No Kakao / TAGO / Groq calls. Coordinates come only from schedule points and
already-loaded place candidates.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from models.access import AccessPoint
from models.place import PlaceCandidate
from models.schedule import ReturnStatus, ScheduleItem, TripDaySchedule, TripSchedule
from models.transport import TransportCandidate, TransportType
from models.trip_request import TripRequest

TimelineKind = Literal[
    "ARRIVAL", "DEPARTURE", "ACTIVITY", "TRAVEL", "FREE_TIME",
    "RETURN_PREP", "RETURN_HUB", "RETURN_TRANSPORT", "RETURN_ACCESS", "RETURN_DONE",
    "OUTBOUND",
]

MarkerRole = Literal[
    "START", "ARRIVAL_HUB", "ACTIVITY", "MAIN", "ACCOMMODATION",
    "PROVISIONAL", "RETURN_HUB",
]

TRAVEL_MODE_LABELS = {
    "BUS": "버스", "SUBWAY": "지하철", "WALKING": "도보",
    "TRAIN": "열차", "EXPRESS_BUS": "고속버스", "INTERCITY_BUS": "시외버스",
}


class MapMarker(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: MarkerRole
    place_id: str
    name: str
    latitude: float
    longitude: float
    sequence: int | None = None  # Activity visit order; hubs/lodging may be None
    is_main: bool = False


class TimelineEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: TimelineKind
    start: datetime
    end: datetime | None = None
    title: str
    detail: str = ""
    caption: str = ""
    is_main: bool = False
    sequence: int | None = None
    place_id: str = ""


class DaySummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    lines: tuple[str, ...] = ()


class ReturnViewSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    goal: datetime | None = None
    expected: datetime | None = None
    margin_minutes: float | None = None
    cutoff: datetime | None = None


class DayView(BaseModel):
    """One tab's visualization payload — destination-day focused (no Seoul↔Gumi map)."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    day_index: int
    date: object
    role: Literal["FIRST", "MIDDLE", "FINAL", "SINGLE"]
    summary: DaySummary
    markers: tuple[MapMarker, ...] = ()
    timeline: tuple[TimelineEvent, ...] = ()
    order_line: tuple[tuple[float, float], ...] = ()  # lat/lon visit-order guide (not real geometry)
    missing_coord_names: tuple[str, ...] = ()
    return_summary: ReturnViewSummary | None = None
    lodging_label: str = ""  # "숙소" or "임시 기준점"
    lodging_name: str = ""


def simplify_category(category: str) -> str:
    parts = [p.strip() for p in (category or "").split(">") if p.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    # Prefer last two leaf labels, drop generic roots like 여행/음식점 when deeper exists.
    leaf = parts[-1]
    parent = parts[-2]
    if parent in {"음식점", "여행", "가정,생활", "서비스,산업"} and leaf:
        return leaf
    return f"{parent} · {leaf}"


def lodging_point_for_day(schedule: TripSchedule, day: TripDaySchedule,
                          *, boundary: Literal["start", "end"]) -> AccessPoint | None:
    """Read overnight lodging from accommodation_nights when present."""
    nights = schedule.accommodation_nights
    idx = day.day_index - 1  # 0-based day
    if boundary == "end":
        if day.role == "FINAL":
            return None
        if nights and 0 <= idx < len(nights):
            return nights[idx]
        return schedule.accommodation
    # start
    if day.role == "FIRST":
        return None
    if nights and idx >= 1 and idx - 1 < len(nights):
        return nights[idx - 1]
    return schedule.accommodation


def lodging_ui_label(status: str) -> str:
    return "임시 기준점" if status == "PROVISIONAL" else "숙소"


def collect_coordinates(schedule: TripSchedule,
                        candidates: list[PlaceCandidate] | None = None) -> dict[str, tuple[float, float]]:
    """lat, lon keyed by place/access id — schedule + already-loaded candidates only."""
    coords: dict[str, tuple[float, float]] = {}
    for candidate in candidates or ():
        coords[candidate.place_id] = (candidate.latitude, candidate.longitude)
    points: list[AccessPoint] = [schedule.arrival_point]
    if schedule.accommodation is not None:
        points.append(schedule.accommodation)
    points.extend(schedule.accommodation_nights)
    for day in schedule.days:
        points.append(day.start_location)
        points.append(day.end_location)
    for item in schedule.items:
        if item.origin is not None:
            points.append(item.origin)
        if item.destination is not None:
            points.append(item.destination)
    for day in schedule.days:
        for item in day.items:
            if item.origin is not None:
                points.append(item.origin)
            if item.destination is not None:
                points.append(item.destination)
    for point in points:
        coords[point.id] = (point.y, point.x)
    return coords


def _travel_label(item: ScheduleItem) -> str:
    if item.reason == "숙소 이동":
        return "숙소 이동"
    modes = " + ".join(TRAVEL_MODE_LABELS.get(m, m) for m in item.travel_mode) or "이동"
    return modes


def _activity_detail(item: ScheduleItem) -> str:
    bits = []
    if item.meal_slot and ":" in item.meal_slot:
        bits.append(item.meal_slot.split(":", 1)[1])  # 점심 / 저녁
    elif item.meal_slot:
        bits.append(item.meal_slot)
    category = simplify_category(item.reason.split(" · ")[0] if " · " in item.reason else "")
    # reason often: "점심 · 음식점 > … · 맛집 성향 후보" or "category · preference"
    if not category and item.reason:
        first = item.reason.split(" · ")[0]
        if first not in {"점심", "저녁"}:
            category = simplify_category(first)
    if category:
        bits.append(category)
    elif item.preference:
        bits.append(item.preference)
    minutes = int(round(item.duration_minutes))
    bits.append(f"체류 {minutes}분")
    return " · ".join(bits)


def _add_marker(markers: dict[str, MapMarker], *, role: MarkerRole, point: AccessPoint,
                sequence: int | None = None, is_main: bool = False) -> None:
    existing = markers.get(point.id)
    if existing is None:
        markers[point.id] = MapMarker(
            role=role, place_id=point.id, name=point.name,
            latitude=point.y, longitude=point.x, sequence=sequence, is_main=is_main)
        return
    # Prefer MAIN / numbered activity over generic roles; keep first sequence.
    role_rank = {"MAIN": 5, "ACTIVITY": 4, "ACCOMMODATION": 3, "PROVISIONAL": 3,
                 "RETURN_HUB": 2, "ARRIVAL_HUB": 2, "START": 1}
    keep_role = existing.role if role_rank.get(existing.role, 0) >= role_rank.get(role, 0) else role
    markers[point.id] = existing.model_copy(update={
        "role": keep_role,
        "sequence": existing.sequence if existing.sequence is not None else sequence,
        "is_main": existing.is_main or is_main,
    })


def build_outbound_timeline(selected: TransportCandidate | None) -> list[TimelineEvent]:
    if selected is None:
        return []
    events: list[TimelineEvent] = []
    access = getattr(selected, "access", None)
    if access is not None and getattr(access, "access_leg", None) is not None:
        leg = access.access_leg
        events.append(TimelineEvent(
            kind="OUTBOUND", start=leg.departure_time, end=leg.arrival_time,
            title=f"{leg.origin} → {leg.destination}",
            detail=f"출발지 접근 · 약 {int(round(leg.duration_minutes))}분"))
    transport_label = TRAVEL_MODE_LABELS.get(
        selected.transport_type.value if isinstance(selected.transport_type, TransportType)
        else str(selected.transport_type),
        selected.transport_type.value if hasattr(selected.transport_type, "value") else "교통편")
    events.append(TimelineEvent(
        kind="OUTBOUND", start=selected.departure_time, end=selected.arrival_time,
        title=f"{selected.departure_place} → {selected.arrival_place}",
        detail=f"{transport_label} · 약 {int(round(selected.duration_minutes))}분"))
    events.append(TimelineEvent(
        kind="ARRIVAL", start=selected.arrival_time, end=None,
        title=f"{selected.arrival_place} 도착",
        detail="목적지 도착"))
    return events


def build_day_view(
        schedule: TripSchedule,
        day: TripDaySchedule | None,
        *,
        trip: TripRequest | None = None,
        selected: TransportCandidate | None = None,
        candidates: list[PlaceCandidate] | None = None,
) -> DayView:
    """Pure transform — never calls external providers."""
    coords = collect_coordinates(schedule, candidates)
    preferred = schedule.user_selected_place_id
    status = schedule.accommodation_status
    label = lodging_ui_label(status)
    markers: dict[str, MapMarker] = {}
    timeline: list[TimelineEvent] = []
    missing: list[str] = []
    order_line: list[tuple[float, float]] = []
    activity_seq = 0

    if day is None:
        # Single-day schedule flattened into one view.
        role: Literal["FIRST", "MIDDLE", "FINAL", "SINGLE"] = "SINGLE"
        day_index = 1
        day_date = schedule.trip_start_datetime.date()
        items = schedule.items
        start_point = schedule.arrival_point
        end_point = None
        activity_start = schedule.trip_start_datetime
        activity_end = items[-1].end_datetime if items else schedule.trip_start_datetime
    else:
        role = day.role
        day_index = day.day_index
        day_date = day.date
        items = day.items
        start_point = day.start_location
        end_point = day.end_location if day.role != "FINAL" else None
        activity_start = day.activity_start
        activity_end = day.activity_end

    overnight_start = lodging_point_for_day(schedule, day, boundary="start") if day else None
    overnight_end = lodging_point_for_day(schedule, day, boundary="end") if day else None

    # --- Outbound + day open ---
    if role in {"FIRST", "SINGLE"}:
        timeline.extend(build_outbound_timeline(selected))
        timeline.append(TimelineEvent(
            kind="ARRIVAL", start=activity_start, end=None,
            title=f"{start_point.name} 도착",
            detail="DAY 일정 시작" if role == "FIRST" else "일정 시작",
            place_id=start_point.id))
        _add_marker(markers, role="ARRIVAL_HUB", point=start_point)
    else:
        base = overnight_start or start_point
        title = f"{base.name} 출발" if status != "PROVISIONAL" else f"{base.name} 임시 기준점 출발"
        timeline.append(TimelineEvent(
            kind="DEPARTURE", start=activity_start, end=None,
            title=title, detail=label + " 출발", place_id=base.id))
        _add_marker(
            markers,
            role="PROVISIONAL" if status == "PROVISIONAL" else "ACCOMMODATION",
            point=base)

    previous = activity_start
    for index, item in enumerate(items):
        if item.start_datetime > previous:
            gap = (item.start_datetime - previous).total_seconds() / 60
            timeline.append(TimelineEvent(
                kind="FREE_TIME", start=previous, end=item.start_datetime,
                title=f"자유시간 · {int(round(gap))}분",
                detail="휴식, 주변 산책 또는 다음 일정 이동에 사용할 수 있는 시간"))
        if item.item_type == "TRAVEL":
            origin_name = item.origin.name if item.origin else ""
            dest_name = item.destination.name if item.destination else item.place_name
            timeline.append(TimelineEvent(
                kind="TRAVEL", start=item.start_datetime, end=item.end_datetime,
                title=f"{origin_name} → {dest_name}",
                detail=f"{_travel_label(item)} · 약 {int(round(item.duration_minutes))}분",
                place_id=item.place_id))
        else:
            activity_seq += 1
            is_main = bool(preferred and item.place_id == preferred)
            title = f"★ {item.place_name}" if is_main else item.place_name
            caption = ""
            if any("영업시간 확인 필요" in c for c in item.checks):
                caption = "영업시간 확인 필요"
            timeline.append(TimelineEvent(
                kind="ACTIVITY", start=item.start_datetime, end=item.end_datetime,
                title=title, detail=_activity_detail(item), caption=caption,
                is_main=is_main, sequence=activity_seq, place_id=item.place_id))
            latlon = coords.get(item.place_id)
            if latlon is None:
                inbound = next(
                    (i.destination for i in reversed(items[:index])
                     if i.item_type == "TRAVEL" and i.destination is not None
                     and i.destination.id == item.place_id),
                    None)
                if inbound is not None:
                    latlon = (inbound.y, inbound.x)
            if latlon is None and item.destination is not None:
                latlon = (item.destination.y, item.destination.x)
            if latlon is None:
                missing.append(item.place_name)
            else:
                point = AccessPoint(
                    id=item.place_id, name=item.place_name,
                    x=latlon[1], y=latlon[0])
                _add_marker(
                    markers,
                    role="MAIN" if is_main else "ACTIVITY",
                    point=point, sequence=activity_seq, is_main=is_main)
                order_line.append(latlon)
        previous = item.end_datetime

    # --- Day close / lodging / return ---
    return_summary = None
    lodging_name = ""
    if role != "FINAL" and role != "SINGLE":
        lodge = overnight_end or end_point or schedule.accommodation
        if lodge is not None:
            lodging_name = lodge.name
            arrive_title = (
                f"{lodge.name} 임시 기준점 도착" if status == "PROVISIONAL"
                else f"{lodge.name} 도착")
            timeline.append(TimelineEvent(
                kind="ARRIVAL", start=previous, end=None,
                title=arrive_title, detail=f"{label} 도착", place_id=lodge.id))
            _add_marker(
                markers,
                role="PROVISIONAL" if status == "PROVISIONAL" else "ACCOMMODATION",
                point=lodge)
    elif role == "FINAL" or (role == "SINGLE" and schedule.return_journey):
        timeline.append(TimelineEvent(
            kind="RETURN_PREP", start=previous, end=None,
            title="귀가 시작 준비", detail="목적지 활동 종료"))
        journey = schedule.return_journey
        if journey is not None:
            hub_leg = journey.to_hub
            timeline.append(TimelineEvent(
                kind="RETURN_HUB", start=hub_leg.departure_time, end=hub_leg.arrival_time,
                title=f"{hub_leg.origin} → {hub_leg.destination}",
                detail=f"Return Hub 이동 · 약 {int(round(hub_leg.duration_minutes))}분"))
            timeline.append(TimelineEvent(
                kind="RETURN_PREP", start=hub_leg.arrival_time, end=None,
                title=f"{hub_leg.destination} 도착",
                detail=f"승차 준비 {int(round(journey.boarding_buffer_minutes))}분"))
            # Return hub marker from destination_point when present.
            hub_point = hub_leg.destination_point
            if hub_point is not None:
                _add_marker(markers, role="RETURN_HUB", point=hub_point)
            transport = journey.transport
            t_label = TRAVEL_MODE_LABELS.get(
                transport.transport_type.value, transport.transport_type.value)
            timeline.append(TimelineEvent(
                kind="RETURN_TRANSPORT", start=transport.departure_time, end=transport.arrival_time,
                title=f"{transport.departure_place} → {transport.arrival_place}",
                detail=f"{t_label} · 약 {int(round(transport.duration_minutes))}분"))
            home = journey.to_origin
            timeline.append(TimelineEvent(
                kind="RETURN_ACCESS", start=home.departure_time, end=home.arrival_time,
                title=f"{home.origin} → {home.destination}",
                detail=f"출발지 이동 · 약 {int(round(home.duration_minutes))}분"))
            done_at = schedule.final_arrival_datetime or home.arrival_time
            timeline.append(TimelineEvent(
                kind="RETURN_DONE", start=done_at, end=None,
                title="귀가 완료", detail=""))
            margin = None
            if schedule.final_arrival_datetime is not None:
                margin = (schedule.trip_end_datetime - schedule.final_arrival_datetime).total_seconds() / 60
            return_summary = ReturnViewSummary(
                goal=schedule.trip_end_datetime,
                expected=schedule.final_arrival_datetime,
                margin_minutes=margin,
                cutoff=schedule.destination_activity_cutoff)
        elif schedule.return_status in {
                ReturnStatus.RETURN_UNKNOWN, ReturnStatus.RETURN_INFEASIBLE, ReturnStatus.RETURN_NONE}:
            return_summary = ReturnViewSummary(
                goal=schedule.trip_end_datetime, expected=None, margin_minutes=None,
                cutoff=schedule.destination_activity_cutoff)

    # Summary lines from existing times only.
    visits = sum(1 for i in items if i.item_type != "TRAVEL")
    summary_lines: list[str] = []
    if role in {"FIRST", "SINGLE"}:
        summary_lines.append(f"{activity_start:%H:%M} {start_point.name} 도착")
    else:
        base = overnight_start or start_point
        summary_lines.append(f"{activity_start:%H:%M} 일정 시작 · {base.name}")
    summary_lines.append(f"방문 장소 {visits}곳")
    if role == "FINAL":
        summary_lines.append(f"{activity_end:%H:%M} 목적지 활동 종료")
        if schedule.return_journey is not None:
            summary_lines.append(
                f"{schedule.return_journey.transport.departure_time:%H:%M} 귀가 "
                f"{TRAVEL_MODE_LABELS.get(schedule.return_journey.transport.transport_type.value, '교통편')}")
        if schedule.final_arrival_datetime is not None:
            summary_lines.append(f"{schedule.final_arrival_datetime:%H:%M} 예상 귀가")
    else:
        summary_lines.append(f"{activity_end:%H:%M} 일정 종료")
        lodge = overnight_end or schedule.accommodation
        if lodge is not None:
            lodging_name = lodge.name
            if status == "PROVISIONAL":
                summary_lines.append(f"임시 기준점: {lodge.name}")
            else:
                summary_lines.append(f"숙소: {lodge.name}" if role == "FIRST" else "숙소 복귀")

    return DayView(
        day_index=day_index, date=day_date, role=role,
        summary=DaySummary(lines=tuple(summary_lines)),
        markers=tuple(markers.values()),
        timeline=tuple(timeline),
        order_line=tuple(order_line),
        missing_coord_names=tuple(dict.fromkeys(missing)),
        return_summary=return_summary,
        lodging_label=label,
        lodging_name=lodging_name,
    )


def build_schedule_views(
        schedule: TripSchedule, *,
        trip: TripRequest | None = None,
        selected: TransportCandidate | None = None,
        candidates: list[PlaceCandidate] | None = None,
) -> tuple[DayView, ...]:
    if schedule.days:
        return tuple(
            build_day_view(schedule, day, trip=trip, selected=selected, candidates=candidates)
            for day in schedule.days)
    return (build_day_view(schedule, None, trip=trip, selected=selected, candidates=candidates),)
