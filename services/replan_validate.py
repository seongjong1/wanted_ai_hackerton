"""Deterministic replan validation — never delegated to LLM."""
from __future__ import annotations

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
    return (not errors, tuple(errors))


def _flat_items(schedule: TripSchedule) -> tuple[ScheduleItem, ...]:
    if schedule.days:
        return tuple(item for day in schedule.days for item in day.items)
    return schedule.items
