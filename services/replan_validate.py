"""Deterministic replan validation — never delegated to LLM."""
from __future__ import annotations

from datetime import timedelta

from models.replan import item_key
from models.schedule import ScheduleItem, TripSchedule


def completed_prefix_unchanged(
        original: TripSchedule,
        proposed: TripSchedule,
        locked_keys: frozenset[str],
) -> bool:
    """Every locked item must appear in proposed with identical fields."""
    proposed_by_key = {item_key(i): i for i in _flat_items(proposed)}
    for item in _flat_items(original):
        key = item_key(item)
        if key not in locked_keys:
            continue
        other = proposed_by_key.get(key)
        if other is None or other != item:
            return False
    return True


def no_duplicate_place_visits(schedule: TripSchedule) -> bool:
    """Reject duplicate PLACE/MEAL visits within the same calendar day."""
    if schedule.days:
        for day in schedule.days:
            seen: set[str] = set()
            for item in day.items:
                if item.item_type == "TRAVEL":
                    continue
                if item.place_id in seen:
                    return False
                seen.add(item.place_id)
        return True
    seen = set()
    for item in schedule.items:
        if item.item_type == "TRAVEL":
            continue
        if item.place_id in seen:
            return False
        seen.add(item.place_id)
    return True


def chronological(schedule: TripSchedule) -> bool:
    """Within each day (or flat items), intervals must be non-overlapping in order."""
    groups = [day.items for day in schedule.days] if schedule.days else [schedule.items]
    for items in groups:
        if not items:
            continue
        prev_end = items[0].start_datetime
        for item in items:
            if item.start_datetime < prev_end:
                return False
            if item.end_datetime <= item.start_datetime:
                return False
            prev_end = item.end_datetime
    return True


def activity_place_ids(schedule: TripSchedule) -> set[str]:
    return {
        i.place_id for i in _flat_items(schedule) if i.item_type != "TRAVEL"
    }


def _terminal_ids(schedule: TripSchedule) -> set[str]:
    ids: set[str] = set()
    if schedule.accommodation:
        ids.add(schedule.accommodation.id)
    ids.update(n.id for n in schedule.accommodation_nights)
    if schedule.arrival_point:
        ids.add(schedule.arrival_point.id)
    if schedule.return_journey and schedule.return_journey.to_hub.destination_point:
        ids.add(schedule.return_journey.to_hub.destination_point.id)
    return ids


def no_orphan_travels(schedule: TripSchedule) -> bool:
    """TRAVEL destinations must match the next activity or a lodging/hub terminal."""
    terminals = _terminal_ids(schedule)
    groups = [day.items for day in schedule.days] if schedule.days else [schedule.items]
    for items in groups:
        for index, item in enumerate(items):
            if item.item_type != "TRAVEL":
                continue
            dest_id = item.destination.id if item.destination else item.place_id
            next_activity = None
            for later in items[index + 1:]:
                if later.item_type != "TRAVEL":
                    next_activity = later
                    break
            if next_activity is not None:
                if dest_id != next_activity.place_id:
                    return False
            elif dest_id not in terminals:
                return False
            # Origin should match previous activity when present
            if item.origin is not None and index > 0:
                prev_activity = None
                for earlier in reversed(items[:index]):
                    if earlier.item_type != "TRAVEL":
                        prev_activity = earlier
                        break
                if prev_activity is not None and item.origin.id != prev_activity.place_id:
                    # Allow chain from previous travel destination
                    prev_travel = items[index - 1] if items[index - 1].item_type == "TRAVEL" else None
                    if prev_travel and prev_travel.destination and prev_travel.destination.id == item.origin.id:
                        pass
                    elif item.origin.id in terminals:
                        pass
                    else:
                        return False
    return True


def return_deadline_ok(schedule: TripSchedule) -> bool:
    if schedule.final_arrival_datetime is None:
        return True
    return schedule.final_arrival_datetime <= schedule.trip_end_datetime


def accommodation_boundaries_ok(schedule: TripSchedule) -> bool:
    """Multi-day schedules must keep overnight lodging as day start/end anchors."""
    if not schedule.days or len(schedule.days) < 2:
        return True
    if schedule.accommodation is None:
        return False
    if not schedule.accommodation_nights:
        return False
    expected = len(schedule.days) - 1
    if len(schedule.accommodation_nights) != expected:
        return False
    lodge = schedule.accommodation
    if schedule.days[0].end_location is None or schedule.days[0].end_location.id != lodge.id:
        return False
    for day in schedule.days[1:]:
        if day.start_location is None or day.start_location.id != lodge.id:
            return False
    for day in schedule.days[:-1]:
        if day.end_location is None or day.end_location.id != lodge.id:
            return False
    return True


def no_overnight_free_gap(schedule: TripSchedule) -> bool:
    """Reject long gaps that cross midnight inside a day (should be lodging boundary)."""
    groups = [day.items for day in schedule.days] if schedule.days else [schedule.items]
    for items in groups:
        previous = None
        for item in items:
            if previous is not None and previous.end_datetime.date() < item.start_datetime.date():
                if (item.start_datetime - previous.end_datetime).total_seconds() >= 4 * 3600:
                    return False
            previous = item
    return True


def _last_non_travel(schedule: TripSchedule) -> ScheduleItem | None:
    if schedule.days:
        final = next((d for d in reversed(schedule.days) if d.role == "FINAL"), schedule.days[-1])
        items = final.items
    else:
        items = schedule.items
    for item in reversed(items):
        if item.item_type != "TRAVEL":
            return item
    return None


def return_access_synced(schedule: TripSchedule) -> bool:
    """Destination-side return access must originate from last activity (or empty day)."""
    journey = schedule.return_journey
    if journey is None:
        return True
    last = _last_non_travel(schedule)
    leg = journey.to_hub
    if leg.origin_point is None:
        return False
    if last is None:
        # No activity — origin may be lodging / arrival / start
        return True
    if leg.origin_point.id != last.place_id:
        return False
    if leg.departure_time < last.end_datetime:
        return False
    hub = leg.destination_point
    if schedule.return_journey and hub is None:
        return False
    return True


def return_boarding_feasible(schedule: TripSchedule) -> bool:
    journey = schedule.return_journey
    if journey is None:
        return True
    leg = journey.to_hub
    ready = leg.arrival_time + timedelta(minutes=journey.boarding_buffer_minutes)
    if ready > journey.transport.departure_time:
        return False
    home = journey.to_origin
    if home.departure_time < journey.transport.arrival_time:
        return False
    if schedule.final_arrival_datetime is not None:
        if schedule.final_arrival_datetime != home.arrival_time:
            return False
    return True


def return_chain_chronological(schedule: TripSchedule) -> bool:
    """Cross-section: last activity → return access → transport → home access."""
    journey = schedule.return_journey
    if journey is None:
        return True
    last = _last_non_travel(schedule)
    a, transport, b = journey.to_hub, journey.transport, journey.to_origin
    if last is not None and a.departure_time < last.end_datetime:
        return False
    if a.arrival_time + timedelta(minutes=journey.boarding_buffer_minutes) > transport.departure_time:
        return False
    if transport.arrival_time < transport.departure_time:
        return False
    if b.departure_time < transport.arrival_time:
        return False
    if schedule.final_arrival_datetime and schedule.final_arrival_datetime > schedule.trip_end_datetime:
        return False
    return True


def validate_replan(
        original: TripSchedule,
        proposed: TripSchedule,
        locked_keys: frozenset[str],
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    if not completed_prefix_unchanged(original, proposed, locked_keys):
        errors.append("completed_mutation")
    if not no_duplicate_place_visits(proposed):
        errors.append("duplicate_place")
    if not chronological(proposed):
        errors.append("chronological")
    if not return_deadline_ok(proposed):
        errors.append("return_deadline")
    if not no_orphan_travels(proposed):
        errors.append("orphan_travel")
    if not accommodation_boundaries_ok(proposed):
        errors.append("accommodation_boundary")
    if not no_overnight_free_gap(proposed):
        errors.append("overnight_free_gap")
    if not return_access_synced(proposed):
        errors.append("return_access_stale")
    if not return_boarding_feasible(proposed):
        errors.append("return_boarding")
    if not return_chain_chronological(proposed):
        errors.append("return_chronology")
    return (not errors, tuple(errors))


def _flat_items(schedule: TripSchedule) -> tuple[ScheduleItem, ...]:
    if schedule.days:
        return tuple(item for day in schedule.days for item in day.items)
    return schedule.items
