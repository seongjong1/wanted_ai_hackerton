"""Phase 6.1 Deterministic Replanning Core — structured events only, no LLM."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from config import ScheduleSettings, Settings
from models.access import AccessPoint
from models.replan import (
    ItemProgress,
    LocationStatus,
    ReplanAnchor,
    ReplanContext,
    ReplanEvent,
    ReplanEventType,
    ReplanResult,
    ReplanStatus,
    item_key,
)
from models.schedule import ScheduleItem, TripDaySchedule, TripSchedule
from services.replan_validate import validate_replan, _flat_items

logger = logging.getLogger("travel_ai.replan")


@dataclass
class _Effects:
    exclude_place_ids: set[str] = field(default_factory=set)
    delay_minutes: int = 0
    fatigue: bool = False
    preference_changes: tuple[str, ...] = ()
    target_meal_role: str = ""  # LUNCH | DINNER | ""
    reject_main: bool = False
    closed_in_progress: bool = False
    reasons: list[str] = field(default_factory=list)


def classify_item(
        item: ScheduleItem,
        current: datetime,
        completed_ids: frozenset[str],
) -> ItemProgress:
    key = item_key(item)
    if key in completed_ids:
        return ItemProgress.COMPLETED
    if item.end_datetime <= current:
        return ItemProgress.COMPLETED
    if item.start_datetime <= current < item.end_datetime:
        return ItemProgress.IN_PROGRESS
    return ItemProgress.FUTURE


def _visit_point(item: ScheduleItem) -> AccessPoint | None:
    if item.item_type == "TRAVEL":
        return item.destination
    return item.destination or item.origin


def build_anchor(context: ReplanContext) -> ReplanAnchor:
    schedule = context.original_schedule
    current = context.current_datetime
    event = context.event
    items = _flat_items(schedule)

    day_index = 1
    if schedule.days:
        for day in schedule.days:
            if day.date == current.date():
                day_index = day.day_index
                break
            if day.date < current.date():
                day_index = day.day_index

    location: AccessPoint | None = None
    status = LocationStatus.UNKNOWN

    if (event.current_latitude is not None and event.current_longitude is not None
            and event.event_type == ReplanEventType.CURRENT_LOCATION_CHANGED):
        location = AccessPoint(
            id="replan-gps", name="현재 위치",
            x=event.current_longitude, y=event.current_latitude, source="USER_REPORTED")
        status = LocationStatus.KNOWN
    elif context.current_location is not None:
        location = context.current_location
        status = LocationStatus.KNOWN
    else:
        # Infer from last completed / in-progress visit (never invent GPS).
        inferred: AccessPoint | None = None
        for item in items:
            progress = classify_item(item, current, context.completed_item_ids)
            if progress == ItemProgress.COMPLETED and item.item_type != "TRAVEL":
                inferred = _visit_point(item) or inferred
            elif progress == ItemProgress.IN_PROGRESS:
                inferred = _visit_point(item) or inferred
                break
        if inferred is not None:
            location = inferred
            status = LocationStatus.INFERRED

    return ReplanAnchor(
        current_datetime=current,
        current_location=location,
        current_day_index=day_index,
        location_status=status,
    )


def _apply_event(context: ReplanContext, progress_map: dict[str, ItemProgress]) -> _Effects:
    event = context.event
    effects = _Effects()
    main_id = context.original_schedule.user_selected_place_id

    if event.event_type in {ReplanEventType.USER_SKIP, ReplanEventType.PLACE_CLOSED}:
        pid = event.place_id
        if not pid:
            effects.reasons.append("missing_place_id")
            return effects
        # Find matching items
        matched = [
            i for i in _flat_items(context.original_schedule)
            if i.place_id == pid and i.item_type != "TRAVEL"
        ]
        if not matched:
            effects.reasons.append("place_not_in_schedule")
            return effects
        # Completed lock: cannot skip/mutate completed visits
        if all(progress_map.get(item_key(i)) == ItemProgress.COMPLETED for i in matched):
            effects.reasons.append("skip_ignored_completed")
            return effects
        in_progress = any(
            progress_map.get(item_key(i)) == ItemProgress.IN_PROGRESS for i in matched)
        future = any(
            progress_map.get(item_key(i)) == ItemProgress.FUTURE for i in matched)
        if not future and not in_progress:
            effects.reasons.append("skip_ignored_completed")
            return effects
        if event.event_type == ReplanEventType.PLACE_CLOSED and in_progress:
            effects.closed_in_progress = True
            effects.exclude_place_ids.add(pid)
            effects.reasons.append("place_closed_in_progress")
        else:
            effects.exclude_place_ids.add(pid)
            effects.reasons.append(
                "user_skip" if event.event_type == ReplanEventType.USER_SKIP else "place_closed")
            if in_progress and event.event_type == ReplanEventType.USER_SKIP:
                effects.closed_in_progress = True  # unlock current activity
        if main_id and pid == main_id and event.event_type == ReplanEventType.USER_SKIP:
            effects.reject_main = True

    elif event.event_type == ReplanEventType.DELAY:
        minutes = int(event.delay_minutes or 0)
        effects.delay_minutes = max(0, minutes)
        effects.reasons.append(f"delay_{effects.delay_minutes}")

    elif event.event_type == ReplanEventType.FATIGUE:
        effects.fatigue = True
        effects.reasons.append("fatigue")

    elif event.event_type == ReplanEventType.CHANGE_PREFERENCE:
        effects.preference_changes = event.preference_changes
        effects.reasons.append("preference_change")

    elif event.event_type == ReplanEventType.MEAL_CHANGE:
        role = (event.target_meal_role or "DINNER").upper()
        if role not in {"LUNCH", "DINNER", "FLEXIBLE", "NONE"}:
            role = "DINNER"
        effects.target_meal_role = role
        effects.reasons.append(f"meal_change_{role.lower()}")

    elif event.event_type == ReplanEventType.CURRENT_LOCATION_CHANGED:
        effects.reasons.append("location_changed")

    else:
        effects.reasons.append(f"unsupported_{event.event_type.value}")

    return effects


def _locked_keys(
        items: tuple[ScheduleItem, ...],
        progress_map: dict[str, ItemProgress],
        effects: _Effects,
) -> frozenset[str]:
    """COMPLETED items stay locked. IN_PROGRESS stays locked unless closed-in-progress.

    Travel into an excluded place is locked only when its following visit was truly completed.
    """
    locked: set[str] = set()
    for index, item in enumerate(items):
        key = item_key(item)
        progress = progress_map[key]

        if item.item_type == "TRAVEL" and item.destination:
            dest_id = item.destination.id
            if dest_id in effects.exclude_place_ids:
                following_completed = False
                for later in items[index + 1:]:
                    if later.item_type == "TRAVEL":
                        continue
                    if later.place_id == dest_id:
                        following_completed = (
                            progress_map.get(item_key(later)) == ItemProgress.COMPLETED)
                    break
                if not following_completed:
                    continue

        if item.place_id in effects.exclude_place_ids and item.item_type != "TRAVEL":
            if progress != ItemProgress.COMPLETED:
                continue

        if progress == ItemProgress.COMPLETED:
            locked.add(key)
            continue
        if progress == ItemProgress.IN_PROGRESS:
            if item.place_id in effects.exclude_place_ids:
                continue
            locked.add(key)
    for index, item in enumerate(items):
        if item.item_type != "TRAVEL":
            continue
        if item.destination and item.destination.id in effects.exclude_place_ids:
            # Only attach if the following visit is locked
            if index + 1 < len(items) and item_key(items[index + 1]) in locked:
                locked.add(item_key(item))
            continue
        if index + 1 < len(items) and item_key(items[index + 1]) in locked:
            locked.add(item_key(item))
    return frozenset(locked)


def _shift_item(item: ScheduleItem, delta: timedelta) -> ScheduleItem:
    return item.model_copy(update={
        "start_datetime": item.start_datetime + delta,
        "end_datetime": item.end_datetime + delta,
    })


def _meal_to_role(item: ScheduleItem, role: str) -> ScheduleItem:
    """Move a future MAIN meal into lunch or dinner window (same duration)."""
    duration = item.end_datetime - item.start_datetime
    if role == "DINNER":
        start = item.start_datetime.replace(hour=17, minute=0, second=0, microsecond=0)
        if start <= item.start_datetime:
            start = item.start_datetime + timedelta(hours=3)
        label = "저녁"
    elif role == "LUNCH":
        start = item.start_datetime.replace(hour=12, minute=0, second=0, microsecond=0)
        if start.date() != item.start_datetime.date():
            start = item.start_datetime.replace(hour=12, minute=0, second=0, microsecond=0)
        # If already past lunch window today, keep relative earlier slot when possible
        if start >= item.start_datetime and "저녁" in (item.meal_slot or ""):
            start = item.start_datetime.replace(hour=12, minute=0, second=0, microsecond=0)
            if start >= item.end_datetime:
                start = item.start_datetime - timedelta(hours=4)
        label = "점심"
    else:
        return item
    return item.model_copy(update={
        "start_datetime": start,
        "end_datetime": start + duration,
        "meal_slot": f"{start.date()}:{label}",
        "reason": (item.reason or "") + f" · {label} 재배치",
    })


def _activity_cutoff(schedule: TripSchedule, day: TripDaySchedule | None) -> datetime:
    if day and day.role == "FINAL" and schedule.destination_activity_cutoff:
        return schedule.destination_activity_cutoff
    if day:
        return day.activity_end
    if schedule.destination_activity_cutoff:
        return schedule.destination_activity_cutoff
    return schedule.trip_end_datetime


def _is_lodging_travel(item: ScheduleItem, schedule: TripSchedule) -> bool:
    if item.item_type != "TRAVEL":
        return False
    lodge_ids = set()
    if schedule.accommodation:
        lodge_ids.add(schedule.accommodation.id)
    lodge_ids.update(n.id for n in schedule.accommodation_nights)
    return item.place_id in lodge_ids or (
        item.destination is not None and item.destination.id in lodge_ids)


def _haversine_meters(a: AccessPoint, b: AccessPoint) -> float:
    from math import asin, cos, radians, sin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, (a.y, a.x, b.y, b.x))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6_371_000 * asin(sqrt(h))


def _lookup_cached_travel_minutes(
        schedule: TripSchedule, origin_id: str, dest_id: str,
) -> float | None:
    for item in _flat_items(schedule):
        if item.item_type != "TRAVEL" or not item.origin or not item.destination:
            continue
        if item.origin.id == origin_id and item.destination.id == dest_id:
            minutes = item.travel_duration_minutes
            if minutes and minutes > 0:
                return float(minutes)
    return None


def _estimate_travel_minutes(
        origin: AccessPoint,
        dest: AccessPoint,
        schedule: TripSchedule,
        resolve_route=None,
) -> tuple[float, tuple[str, ...]]:
    """Prefer cache / optional Kakao resolver; else distance estimate. Never invent places."""
    if origin.id == dest.id:
        return 0.0, ()
    cached = _lookup_cached_travel_minutes(schedule, origin.id, dest.id)
    if cached is not None:
        return cached, ("BUS",)
    if resolve_route is not None:
        try:
            route = resolve_route(origin, dest)
            if route is not None and getattr(route, "duration_seconds", 0) > 0:
                modes = tuple(dict.fromkeys(
                    getattr(s, "mode", "BUS") for s in getattr(route, "steps", ()) or ()))
                return float(route.duration_seconds) / 60.0, modes or ("BUS",)
        except Exception:
            logger.info("replan route_resolver_failed origin=%s dest=%s", origin.id, dest.id)
    meters = _haversine_meters(origin, dest)
    # ~18 km/h effective transit + 8 min overhead
    minutes = max(8.0, min(90.0, meters / 300.0 + 8.0))
    return minutes, ("BUS",)


def _make_travel(
        origin: AccessPoint,
        dest: AccessPoint,
        start: datetime,
        schedule: TripSchedule,
        *,
        resolve_route=None,
        reason: str = "재계획 이동",
) -> ScheduleItem | None:
    if origin.id == dest.id:
        return None
    minutes, modes = _estimate_travel_minutes(origin, dest, schedule, resolve_route)
    if minutes <= 0:
        return None
    end = start + timedelta(minutes=minutes)
    return ScheduleItem(
        item_type="TRAVEL",
        place_id=dest.id,
        place_name=dest.name,
        start_datetime=start,
        end_datetime=end,
        origin=origin,
        destination=dest,
        travel_mode=modes or ("BUS",),
        travel_duration_minutes=minutes,
        reason=reason,
        validation_status="VALIDATED",
    )


def _access_point_for_visit(
        item: ScheduleItem,
        schedule: TripSchedule,
        pool_by_id: dict[str, AccessPoint],
) -> AccessPoint | None:
    if item.destination is not None and item.item_type != "TRAVEL":
        # rare
        pass
    for other in _flat_items(schedule):
        if other.item_type != "TRAVEL":
            continue
        if other.destination and other.destination.id == item.place_id:
            return other.destination
        if other.origin and other.origin.id == item.place_id:
            return other.origin
    if item.place_id in pool_by_id:
        return pool_by_id[item.place_id]
    if schedule.accommodation and schedule.accommodation.id == item.place_id:
        return schedule.accommodation
    for night in schedule.accommodation_nights:
        if night.id == item.place_id:
            return night
    if schedule.arrival_point and schedule.arrival_point.id == item.place_id:
        return schedule.arrival_point
    return None


def _point_from_prefix(
        prefix: list[ScheduleItem],
        schedule: TripSchedule,
        anchor: ReplanAnchor,
) -> AccessPoint:
    if anchor.current_location is not None:
        return anchor.current_location
    for item in reversed(prefix):
        if item.item_type == "TRAVEL" and item.destination is not None:
            return item.destination
        point = _access_point_for_visit(item, schedule, {})
        if point is not None:
            return point
    if schedule.accommodation is not None:
        return schedule.accommodation
    return schedule.arrival_point


def _locked_prefix(items: tuple[ScheduleItem, ...], locked_keys: frozenset[str]) -> list[ScheduleItem]:
    prefix: list[ScheduleItem] = []
    for item in items:
        if item_key(item) in locked_keys:
            prefix.append(item)
        else:
            break
    return prefix


def _candidate_point(candidate) -> AccessPoint:
    return AccessPoint(
        id=candidate.place_id,
        name=candidate.place_name,
        x=candidate.longitude,
        y=candidate.latitude,
        address=getattr(candidate, "address", "") or "",
        category=getattr(candidate, "category", "") or "",
        source="PLACE_POOL",
    )


def _rank_pool_candidates(
        pool: tuple,
        *,
        exclude_ids: set[str],
        preference_boost: tuple[str, ...],
        fatigue: bool,
) -> list:
    ranked = []
    for cand in pool:
        if cand.place_id in exclude_ids:
            continue
        score = float(getattr(cand, "preference_score", 0) or 0)
        matched = {p.value if hasattr(p, "value") else str(p)
                   for p in (getattr(cand, "matched_preferences", ()) or ())}
        for pref in preference_boost:
            if pref in matched:
                score += 25
        if fatigue:
            # Prefer nearer / shorter — distance_meters if present
            dist = getattr(cand, "distance_meters", None)
            if dist is not None:
                score -= float(dist) / 200.0
        ranked.append((score, cand))
    ranked.sort(key=lambda row: (-row[0], row[1].place_id))
    return [c for _, c in ranked]


def _visit_from_candidate(candidate, start: datetime, *, preference: str = "") -> ScheduleItem:
    duration = timedelta(minutes=60)
    cat = (getattr(candidate, "category", "") or "")
    is_meal = "음식" in cat or "식당" in cat or "카페" in cat
    end = start + (timedelta(minutes=70) if is_meal else duration)
    meal_slot = None
    if is_meal:
        label = "점심" if start.hour < 15 else "저녁"
        meal_slot = f"{start.date()}:{label}"
    prefs = getattr(candidate, "matched_preferences", ()) or ()
    pref_val = preference or (prefs[0].value if prefs and hasattr(prefs[0], "value") else (prefs[0] if prefs else "관광"))
    return ScheduleItem(
        item_type="MEAL" if is_meal else "PLACE",
        place_id=candidate.place_id,
        place_name=candidate.place_name,
        start_datetime=start,
        end_datetime=end,
        preference=str(pref_val),
        meal_slot=meal_slot,
        reason="재계획 대체 후보 · " + cat,
        checks=("영업시간 확인 필요",),
        validation_status="VALIDATED",
    )


def _prune_remaining_visits(
        visits: list[ScheduleItem],
        *,
        effects: _Effects,
        attempt: int,
        config: ScheduleSettings,
        main_id: str | None,
        schedule: TripSchedule,
        planning_clock: datetime,
) -> tuple[list[ScheduleItem], list[ScheduleItem], str]:
    """Filter/order remaining visits before travel rebuild. Returns (keep, removed, main_status)."""
    removed: list[ScheduleItem] = []
    kept: list[ScheduleItem] = []
    main_status = "unchanged"
    max_future = None
    if effects.fatigue:
        n = len(visits)
        # Cap relative to *remaining* count (not global max_visits), so 4→2 not 4→4.
        if n <= 1:
            max_future = n
        else:
            target = max(1, int(n * config.fatigue_max_visits_factor))
            target = min(target, n - 1)  # always drop at least one when possible
            max_future = max(0, target - (attempt - 1))

    # Preference / fatigue ordering
    ordered = list(visits)
    if effects.fatigue:
        def fatigue_key(item: ScheduleItem) -> tuple:
            is_main = bool(main_id and item.place_id == main_id)
            is_meal = bool(item.meal_slot)
            # Keep MAIN and meals; drop sightseeing / shopping first
            return (0 if is_main else 1, 0 if is_meal else 1, item.start_datetime)

        ordered.sort(key=fatigue_key)
    elif effects.preference_changes:
        boost = set(effects.preference_changes)

        def pref_key(item: ScheduleItem) -> tuple:
            hit = 1 if item.preference in boost else 0
            return (-hit, item.start_datetime)

        ordered.sort(key=pref_key)

    count = 0
    for item in ordered:
        working = item
        day = _day_for_item(schedule, item)
        cutoff = _activity_cutoff(schedule, day)
        if effects.delay_minutes and day and day.role != "FINAL":
            cutoff = cutoff + timedelta(minutes=effects.delay_minutes)

        if (effects.target_meal_role in {"LUNCH", "DINNER"} and main_id
                and working.place_id == main_id
                and working.item_type in {"MEAL", "PLACE"}):
            moved = _meal_to_role(working, effects.target_meal_role)
            if moved.start_datetime != working.start_datetime or moved.meal_slot != working.meal_slot:
                working = moved
                main_status = "moved_meal"

        # Rough feasibility vs planning clock + duration
        duration = working.end_datetime - working.start_datetime
        earliest_end = planning_clock + duration
        if earliest_end > cutoff or working.start_datetime.date() < planning_clock.date() and day and day.date < planning_clock.date():
            removed.append(item)
            if main_id and item.place_id == main_id:
                main_status = "INFEASIBLE"
            continue
        if attempt >= 2 and working.item_type == "PLACE" and not working.meal_slot:
            if not (main_id and working.place_id == main_id):
                removed.append(item)
                continue
        if attempt >= 3 and not working.meal_slot:
            if not (main_id and working.place_id == main_id and main_status != "INFEASIBLE"):
                removed.append(item)
                continue
        if max_future is not None:
            count += 1
            if count > max_future:
                removed.append(item)
                continue
        kept.append(working)
    return kept, removed, main_status


def _chain_visits(
        visits: list[ScheduleItem],
        cursor_point: AccessPoint,
        cursor_time: datetime,
        schedule: TripSchedule,
        day: TripDaySchedule | None,
        effects: _Effects,
        config: ScheduleSettings,
        exclude_ids: set[str],
        pool_points: dict[str, AccessPoint],
        place_pool: tuple,
        resolve_route,
        main_id: str | None,
        main_status: str,
        attempt: int,
) -> tuple[list[ScheduleItem], list[ScheduleItem], list[ScheduleItem], str, datetime, AccessPoint]:
    built: list[ScheduleItem] = []
    removed: list[ScheduleItem] = []
    added: list[ScheduleItem] = []
    cutoff = _activity_cutoff(schedule, day)
    if effects.delay_minutes and day and day.role != "FINAL":
        cutoff = cutoff + timedelta(minutes=effects.delay_minutes)

    def append_visit(visit: ScheduleItem, point: AccessPoint) -> bool:
        nonlocal cursor_point, cursor_time, main_status
        travel = _make_travel(
            cursor_point, point, cursor_time, schedule, resolve_route=resolve_route)
        arrive = travel.end_datetime if travel is not None else cursor_time
        duration = visit.end_datetime - visit.start_datetime
        new_visit = visit.model_copy(update={
            "start_datetime": arrive,
            "end_datetime": arrive + duration,
        })
        if new_visit.end_datetime > cutoff:
            if main_id and visit.place_id == main_id:
                main_status = "INFEASIBLE"
            return False
        if travel is not None:
            built.append(travel)
            added.append(travel)
        if (new_visit.start_datetime != visit.start_datetime
                or new_visit.end_datetime != visit.end_datetime):
            added.append(new_visit)
        built.append(new_visit)
        cursor_point = point
        cursor_time = new_visit.end_datetime
        exclude_ids.add(visit.place_id)
        return True

    for visit in visits:
        point = _access_point_for_visit(visit, schedule, pool_points)
        if point is None:
            removed.append(visit)
            continue
        if not append_visit(visit, point):
            removed.append(visit)

    # Long-gap replacement from place_pool (optional; never invent places)
    if not effects.fatigue and attempt <= 2 and place_pool:
        gap_minutes = (cutoff - cursor_time).total_seconds() / 60
        if gap_minutes >= config.min_supporting_activity_gap_minutes:
            limit = 2 if gap_minutes >= config.long_gap_minutes else 1
            ranked = _rank_pool_candidates(
                place_pool,
                exclude_ids=exclude_ids,
                preference_boost=effects.preference_changes,
                fatigue=False,
            )
            inserted = 0
            for cand in ranked:
                if inserted >= limit:
                    break
                point = pool_points.get(cand.place_id) or _candidate_point(cand)
                travel = _make_travel(
                    cursor_point, point, cursor_time, schedule, resolve_route=resolve_route)
                arrive = travel.end_datetime if travel is not None else cursor_time
                synth = _visit_from_candidate(cand, arrive)
                if synth.end_datetime > cutoff:
                    continue
                # Leave buffer before lodging/return
                if (cutoff - synth.end_datetime).total_seconds() / 60 < 15 and day and day.role != "FINAL":
                    continue
                if travel is not None:
                    built.append(travel)
                    added.append(travel)
                built.append(synth)
                added.append(synth)
                cursor_point = point
                cursor_time = synth.end_datetime
                exclude_ids.add(cand.place_id)
                inserted += 1

    return built, removed, added, main_status, cursor_time, cursor_point


def _rebuild_future_from_anchor(
        context: ReplanContext,
        prefix: list[ScheduleItem],
        remaining_visits: list[ScheduleItem],
        effects: _Effects,
        attempt: int,
        config: ScheduleSettings,
        anchor: ReplanAnchor,
        *,
        resolve_route=None,
) -> tuple[list[ScheduleItem], list[ScheduleItem], list[ScheduleItem], str]:
    """LOCKED prefix + new travel/activity chain from anchor. No orphan travels."""
    schedule = context.original_schedule
    main_id = schedule.user_selected_place_id
    removed: list[ScheduleItem] = []
    added: list[ScheduleItem] = []
    place_pool = tuple(context.place_pool or ())

    pool_points = {c.place_id: _candidate_point(c) for c in place_pool}
    planning_clock = anchor.current_datetime
    if prefix:
        planning_clock = max(planning_clock, prefix[-1].end_datetime)

    kept_visits, pruned, main_status = _prune_remaining_visits(
        remaining_visits, effects=effects, attempt=attempt, config=config,
        main_id=main_id, schedule=schedule, planning_clock=planning_clock)
    removed.extend(pruned)

    exclude_ids = set(effects.exclude_place_ids)
    exclude_ids.update(i.place_id for i in prefix if i.item_type != "TRAVEL")
    exclude_ids.update(i.place_id for i in kept_visits)

    cursor_time = planning_clock
    cursor_point = _point_from_prefix(prefix, schedule, anchor)
    built: list[ScheduleItem] = []

    by_day: dict = {}
    for visit in kept_visits:
        by_day.setdefault(visit.start_datetime.date(), []).append(visit)

    days = list(schedule.days) if schedule.days else []
    if not days:
        synthetic_dates = sorted(by_day.keys()) or [cursor_time.date()]
        for date in synthetic_dates:
            visits = by_day.get(date, [])
            built_part, rem_part, add_part, main_status, cursor_time, cursor_point = _chain_visits(
                visits, cursor_point, cursor_time, schedule, None, effects, config,
                exclude_ids, pool_points, place_pool, resolve_route, main_id, main_status,
                attempt)
            built.extend(built_part)
            removed.extend(rem_part)
            added.extend(add_part)
        return prefix + built, removed, added, main_status

    prefix_keys = {item_key(x) for x in prefix}
    for day in days:
        # Skip days fully covered by locked prefix with no remaining visits
        day_has_unlocked = any(item_key(i) not in prefix_keys for i in day.items)
        visits = by_day.get(day.date, [])
        if not day_has_unlocked and not visits and day.date < cursor_time.date():
            continue
        if day.date < planning_clock.date() and not visits:
            continue

        if day.date > cursor_time.date():
            start_loc = day.start_location
            if start_loc is not None and cursor_point.id != start_loc.id:
                hop = _make_travel(
                    cursor_point, start_loc, cursor_time, schedule,
                    resolve_route=resolve_route, reason="일차 이동")
                if hop is not None:
                    built.append(hop)
                    cursor_time = hop.end_datetime
                    cursor_point = start_loc
            cursor_time = max(cursor_time, day.activity_start)
            if start_loc is not None:
                cursor_point = start_loc
        elif day.date == cursor_time.date() and not visits and not day_has_unlocked:
            continue

        built_part, rem_part, add_part, main_status, cursor_time, cursor_point = _chain_visits(
            visits, cursor_point, cursor_time, schedule, day, effects, config,
            exclude_ids, pool_points, place_pool, resolve_route, main_id, main_status,
            attempt)
        built.extend(built_part)
        removed.extend(rem_part)
        added.extend(add_part)

        if day.role in {"FIRST", "MIDDLE"}:
            end_pt = day.end_location or schedule.accommodation
            if end_pt is not None and cursor_point.id != end_pt.id:
                lodging = _make_travel(
                    cursor_point, end_pt, cursor_time, schedule,
                    resolve_route=resolve_route, reason="숙소 이동")
                night_limit = day.activity_end + timedelta(hours=3)
                if lodging is not None and lodging.end_datetime <= night_limit:
                    built.append(lodging)
                    cursor_time = lodging.end_datetime
                    cursor_point = end_pt
                    added.append(lodging)

    return prefix + built, removed, added, main_status


def _rebuild_attempt(
        context: ReplanContext,
        locked_keys: frozenset[str],
        effects: _Effects,
        attempt: int,
        config: ScheduleSettings,
        anchor: ReplanAnchor,
        *,
        resolve_route=None,
) -> tuple[TripSchedule | None, list[ScheduleItem], list[ScheduleItem], str]:
    """Rebuild FUTURE from anchor: activity sequence first, then derived travels."""
    schedule = context.original_schedule
    items = _flat_items(schedule)
    prefix = _locked_prefix(items, locked_keys)

    removed_early: list[ScheduleItem] = []
    remaining_visits: list[ScheduleItem] = []
    for item in items[len(prefix):]:
        if item.item_type == "TRAVEL":
            # Never keep unlocked historical travels — regenerate after sequence
            removed_early.append(item)
            continue
        if item.place_id in effects.exclude_place_ids:
            removed_early.append(item)
            continue
        remaining_visits.append(item)

    ordered, removed, added, main_status = _rebuild_future_from_anchor(
        context, prefix, remaining_visits, effects, attempt, config, anchor,
        resolve_route=resolve_route)
    removed = list(removed_early) + list(removed)

    if effects.reject_main:
        main_status = "REJECTED_BY_USER"

    proposed = _assemble_schedule(schedule, ordered, main_status)
    return proposed, removed, added, main_status


def _day_for_item(schedule: TripSchedule, item: ScheduleItem) -> TripDaySchedule | None:
    if not schedule.days:
        return None
    for day in schedule.days:
        if any(item_key(i) == item_key(item) for i in day.items):
            return day
        if day.date == item.start_datetime.date():
            return day
    return None


def _assemble_schedule(
        original: TripSchedule,
        ordered: list[ScheduleItem],
        main_status: str,
) -> TripSchedule:
    if not original.days:
        updates: dict = {
            "items": tuple(ordered),
            "validation_status": "VALIDATED",
        }
        if main_status == "INFEASIBLE":
            updates["anchor_status"] = "INFEASIBLE"
        elif main_status == "moved_meal":
            # Infer from items if possible
            role = "DINNER"
            for item in ordered:
                if original.user_selected_place_id and item.place_id == original.user_selected_place_id:
                    if item.meal_slot and "점심" in item.meal_slot:
                        role = "LUNCH"
                    elif item.meal_slot and "저녁" in item.meal_slot:
                        role = "DINNER"
            updates["anchor_meal_role"] = role
            updates["anchor_status"] = original.anchor_status or "INCLUDED"
        elif main_status == "REJECTED_BY_USER":
            updates["user_selected_place_id"] = None
            updates["anchor_status"] = ""
        return original.model_copy(update=updates)

    # Assign each ordered item to a day by calendar date (stable for shifted meals).
    new_days: list[TripDaySchedule] = []
    used: set[str] = set()
    for day in original.days:
        day_items: list[ScheduleItem] = []
        for item in ordered:
            key = item_key(item)
            if key in used:
                continue
            if item.start_datetime.date() != day.date:
                continue
            if item.item_type != "TRAVEL" and any(
                    x.place_id == item.place_id and x.item_type != "TRAVEL" for x in day_items):
                continue
            day_items.append(item)
            used.add(key)
        activity_end = day_items[-1].end_datetime if day_items else day.activity_end
        new_days.append(day.model_copy(update={
            "items": tuple(day_items),
            "activity_end": max(activity_end, day.activity_start),
        }))

    flat = tuple(i for d in new_days for i in d.items)
    updates = {
        "days": tuple(new_days),
        "items": flat,
        "validation_status": "VALIDATED",
    }
    if main_status == "INFEASIBLE":
        updates["anchor_status"] = "INFEASIBLE"
    elif main_status == "moved_meal":
        role = "DINNER"
        for item in ordered:
            if original.user_selected_place_id and item.place_id == original.user_selected_place_id:
                if item.meal_slot and "점심" in item.meal_slot:
                    role = "LUNCH"
                elif item.meal_slot and "저녁" in item.meal_slot:
                    role = "DINNER"
        updates["anchor_meal_role"] = role
        updates["anchor_status"] = original.anchor_status or "INCLUDED"
    elif main_status == "REJECTED_BY_USER":
        updates["user_selected_place_id"] = None
        updates["anchor_status"] = ""
    return original.model_copy(update=updates)


def _future_visit_signature(
        schedule: TripSchedule,
        completed_ids: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for item in _flat_items(schedule):
        if item.item_type == "TRAVEL":
            continue
        if item_key(item) in completed_ids:
            continue
        rows.append((item.place_id, item.meal_slot or ""))
    return tuple(rows)


def _lodging_arrival_datetime(schedule: TripSchedule) -> datetime | None:
    ends = [
        i.end_datetime for i in _flat_items(schedule)
        if i.item_type == "TRAVEL" and (i.reason == "숙소 이동" or _is_lodging_travel(i, schedule))
    ]
    return max(ends) if ends else None


def has_substantive_schedule_change(
        original: TripSchedule,
        proposed: TripSchedule,
        completed_ids: frozenset[str],
) -> bool:
    """True when future places/meals/return/lodging meaningfully differ."""
    if _future_visit_signature(original, completed_ids) != _future_visit_signature(
            proposed, completed_ids):
        return True
    if original.final_arrival_datetime != proposed.final_arrival_datetime:
        return True
    if _lodging_arrival_datetime(original) != _lodging_arrival_datetime(proposed):
        return True
    # Same places but start times shifted by more than 1 minute
    orig = {
        i.place_id: i.start_datetime
        for i in _flat_items(original)
        if i.item_type != "TRAVEL" and item_key(i) not in completed_ids
    }
    for item in _flat_items(proposed):
        if item.item_type == "TRAVEL" or item_key(item) in completed_ids:
            continue
        prev = orig.get(item.place_id)
        if prev is None:
            return True
        if abs((item.start_datetime - prev).total_seconds()) > 60:
            return True
    return False


def replan_trip_schedule(
        context: ReplanContext,
        settings: Settings | ScheduleSettings | None = None,
        *,
        resolve_route=None,
) -> ReplanResult:
    """Deterministic replan entry — never mutates ``context.original_schedule``."""
    config = (
        settings.schedule if isinstance(settings, Settings)
        else settings if isinstance(settings, ScheduleSettings)
        else ScheduleSettings()
    )
    original = context.original_schedule
    items = _flat_items(original)
    progress_map = {
        item_key(i): classify_item(i, context.current_datetime, context.completed_item_ids)
        for i in items
    }
    anchor = build_anchor(context)
    # DELAY moves the planning clock for future rebuild
    effects = _apply_event(context, progress_map)
    if effects.delay_minutes:
        anchor = anchor.model_copy(update={
            "current_datetime": anchor.current_datetime + timedelta(minutes=effects.delay_minutes),
        })

    locked = _locked_keys(items, progress_map, effects)
    notices: list[str] = []
    if "skip_ignored_completed" in effects.reasons:
        notices.append("이미 완료한 일정은 변경할 수 없습니다.")
        return ReplanResult(
            status=ReplanStatus.REPLAN_INFEASIBLE,
            original_schedule=original,
            proposed_schedule=original,
            reasons=tuple(effects.reasons),
            notices=tuple(notices),
            attempts=0,
            anchor=anchor,
            main_status="unchanged",
        )

    last_errors: tuple[str, ...] = ()
    last_proposed: TripSchedule | None = None
    last_removed: list[ScheduleItem] = []
    last_added: list[ScheduleItem] = []
    last_main = "unchanged"
    max_attempts = config.max_replan_attempts

    for attempt in range(1, max_attempts + 1):
        proposed, removed, added, main_status = _rebuild_attempt(
            context, locked, effects, attempt, config, anchor,
            resolve_route=resolve_route)
        if proposed is None:
            continue
        ok, errors = validate_replan(original, proposed, locked)
        last_proposed, last_removed, last_added = proposed, removed, added
        last_main, last_errors = main_status, errors
        if ok:
            if not has_substantive_schedule_change(
                    original, proposed, context.completed_item_ids):
                notice = (
                    "현재 조건에서는 일정을 더 줄이지 않아도 숙소/귀가 조건을 만족합니다."
                    if effects.fatigue else
                    "적용할 수 있는 실질적인 일정 변경이 없습니다."
                )
                return ReplanResult(
                    status=ReplanStatus.REPLAN_NO_CHANGE,
                    original_schedule=original,
                    proposed_schedule=original,
                    removed_items=(),
                    added_items=(),
                    main_status=main_status,
                    reasons=tuple(effects.reasons) + ("no_change",),
                    notices=(notice,),
                    attempts=attempt,
                    anchor=anchor,
                )
            status = (
                ReplanStatus.REPLAN_PARTIAL if removed else ReplanStatus.REPLAN_SUCCESS
            )
            if main_status == "INFEASIBLE":
                notices.append("MAIN_INFEASIBLE: 남은 시간·귀가 제약으로 MAIN을 배치할 수 없습니다.")
                status = ReplanStatus.REPLAN_PARTIAL
            logger.info(
                "replan outcome=%s attempt=%d removed=%d event=%s",
                status.value, attempt, len(removed), context.event.event_type.value)
            return ReplanResult(
                status=status,
                original_schedule=original,
                proposed_schedule=proposed,
                changed_items=tuple(added),
                removed_items=tuple(removed),
                added_items=tuple(added),
                main_status=main_status,
                reasons=tuple(effects.reasons),
                notices=tuple(notices),
                attempts=attempt,
                anchor=anchor,
            )
        logger.info("replan attempt=%d validation_failed=%s", attempt, errors)

    # Bounded fallback: locked prefix + regenerated lodging hop (no orphan travels)
    prefix = _locked_prefix(items, locked)
    empty_anchor = anchor
    ordered, removed_fb, added_fb, main_fb = _rebuild_future_from_anchor(
        context, prefix, [], effects, max_attempts, config, empty_anchor,
        resolve_route=resolve_route)
    fallback = _assemble_schedule(original, ordered, main_fb or last_main or "unchanged")
    ok, errors = validate_replan(original, fallback, locked)
    notices.append("재계획 시도 한도에 도달하여 안전한 최소 일정으로 되돌렸습니다.")
    status = ReplanStatus.REPLAN_PARTIAL if ok else ReplanStatus.REPLAN_FAILED
    if last_errors:
        notices.append(f"validation:{','.join(last_errors)}")
    return ReplanResult(
        status=status,
        original_schedule=original,
        proposed_schedule=fallback if ok else last_proposed,
        removed_items=tuple(last_removed or removed_fb),
        added_items=tuple(last_added or added_fb),
        main_status=last_main,
        reasons=tuple(effects.reasons) + ("max_attempts",),
        notices=tuple(notices),
        attempts=max_attempts,
        anchor=anchor,
    )
