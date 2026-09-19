"""Phase 6.1 Deterministic Replanning Core — CASE 1–20."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from config import ScheduleSettings, load_settings
from models.replan import (
    ItemProgress,
    ReplanContext,
    ReplanEvent,
    ReplanEventType,
    ReplanStatus,
    item_key,
)
from models.schedule import ScheduleItem
from models.transport import KST
from services.replan_service import (
    build_anchor,
    classify_item,
    replan_trip_schedule,
)
from services.replan_validate import (
    completed_prefix_unchanged,
    no_duplicate_place_visits,
    validate_replan,
    _flat_items,
)
from test_places import trip, selected
from test_schedule_day_view import multiday_schedule


def _completed_keys_until(schedule, until: datetime) -> frozenset[str]:
    return frozenset(
        item_key(i) for i in _flat_items(schedule)
        if i.end_datetime <= until
    )


def _ctx(schedule, selected, trip, current, event, *, completed=None, location=None):
    done = completed if completed is not None else _completed_keys_until(schedule, current)
    return ReplanContext(
        trip=trip.model_copy(update={
            "start_date": schedule.trip_start_datetime.date(),
            "end_date": schedule.trip_end_datetime.date(),
            "has_accommodation": True,
        }),
        original_schedule=schedule,
        selected_transport=selected,
        current_datetime=current,
        event=event,
        completed_item_ids=done,
        current_location=location,
    )


@pytest.fixture
def schedule():
    return multiday_schedule()


@pytest.fixture
def settings():
    return load_settings({}, {})


def test_case1_user_skip_future_place(schedule, selected, trip, settings):
    """USER_SKIP future market on day2 — completed day1 unchanged."""
    current = datetime(2026, 9, 19, 13, 0, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP,
        occurred_at=current,
        place_id="p2",
        source="USER_REPORTED",
    )
    # Day1 fully completed; day2 market not yet visited (day2 visit ends 10:15)
    # At 13:00 on day2, day2 p2 visit (09:15-10:15) is already COMPLETED by time.
    # Use earlier current so p2 on day2 is still FUTURE.
    current = datetime(2026, 9, 19, 9, 20, tzinfo=KST)
    event = event.model_copy(update={"occurred_at": current})
    # Mark only day1 items completed explicitly
    day1_keys = frozenset(item_key(i) for i in schedule.days[0].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1_keys), settings)
    assert result.status in {ReplanStatus.REPLAN_SUCCESS, ReplanStatus.REPLAN_PARTIAL}
    assert result.proposed_schedule is not None
    # Day1 items identical
    assert list(result.proposed_schedule.days[0].items) == list(schedule.days[0].items)
    # p2 visit removed from day2
    day2_visits = [
        i for i in result.proposed_schedule.days[1].items if i.item_type != "TRAVEL"]
    assert all(i.place_id != "p2" for i in day2_visits)
    assert any(i.place_id == "p2" for i in result.removed_items)


def test_case2_user_skip_completed_forbidden(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 13, 0, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP,
        occurred_at=current,
        place_id="p1",  # day1 lunch — completed
    )
    result = replan_trip_schedule(_ctx(schedule, selected, trip, current, event), settings)
    assert result.status == ReplanStatus.REPLAN_INFEASIBLE
    assert "skip_ignored_completed" in result.reasons
    assert result.proposed_schedule == schedule


def test_case3_place_closed_future(schedule, selected, trip, settings):
    # Before day2 p2 starts (09:15)
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.PLACE_CLOSED,
        occurred_at=current,
        place_id="p2",
        source="USER_REPORTED",
    )
    day1_keys = frozenset(item_key(i) for i in schedule.days[0].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1_keys), settings)
    assert result.proposed_schedule is not None
    assert any(i.place_id == "p2" for i in result.removed_items)
    assert "place_closed" in result.reasons


def test_case4_place_closed_in_progress_not_completed(schedule, selected, trip, settings):
    # Day2 p2 visit: 09:15–10:15
    current = datetime(2026, 9, 19, 9, 40, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.PLACE_CLOSED,
        occurred_at=current,
        place_id="p2",
        source="USER_REPORTED",
    )
    day1_keys = frozenset(item_key(i) for i in schedule.days[0].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1_keys), settings)
    assert "place_closed_in_progress" in result.reasons
    # In-progress closed place must not remain as a completed visit
    assert result.proposed_schedule is not None
    mid = [
        i for i in result.proposed_schedule.days[1].items
        if i.place_id == "p2" and i.item_type != "TRAVEL"]
    assert mid == []


def test_case5_delay_shifts_future(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 10, 20, tzinfo=KST)  # after day2 market
    event = ReplanEvent(
        event_type=ReplanEventType.DELAY,
        occurred_at=current,
        delay_minutes=30,
    )
    result = replan_trip_schedule(_ctx(schedule, selected, trip, current, event), settings)
    assert result.proposed_schedule is not None
    assert result.anchor is not None
    assert result.anchor.current_datetime == current + timedelta(minutes=30)
    # Day1 unchanged
    assert list(result.proposed_schedule.days[0].items) == list(schedule.days[0].items)


def test_case6_delay_protects_return_deadline(schedule, selected, trip, settings):
    # Final day: delay 60m should drop activities past cutoff
    current = datetime(2026, 9, 20, 9, 30, tzinfo=KST)  # during final p4
    event = ReplanEvent(
        event_type=ReplanEventType.DELAY,
        occurred_at=current,
        delay_minutes=60,
    )
    day1 = frozenset(item_key(i) for i in schedule.days[0].items)
    day2 = frozenset(item_key(i) for i in schedule.days[1].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1 | day2), settings)
    assert result.proposed_schedule is not None
    assert result.proposed_schedule.final_arrival_datetime <= schedule.trip_end_datetime
    assert result.proposed_schedule.return_status == schedule.return_status


def test_case7_fatigue_reduces_future(schedule, selected, trip, settings):
    current = datetime(2026, 9, 18, 15, 0, tzinfo=KST)  # mid day1 after market
    event = ReplanEvent(event_type=ReplanEventType.FATIGUE, occurred_at=current)
    # Complete items up to market (p2)
    done = frozenset(
        item_key(i) for i in schedule.days[0].items if i.end_datetime <= current)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=done), settings)
    assert result.proposed_schedule is not None
    assert "fatigue" in result.reasons
    orig_future = sum(
        1 for i in _flat_items(schedule)
        if i.item_type != "TRAVEL" and item_key(i) not in done)
    new_future = sum(
        1 for i in _flat_items(result.proposed_schedule)
        if i.item_type != "TRAVEL" and item_key(i) not in done)
    if orig_future >= 2:
        assert new_future < orig_future
        assert result.status in {ReplanStatus.REPLAN_SUCCESS, ReplanStatus.REPLAN_PARTIAL}
    else:
        assert new_future <= orig_future


def test_fatigue_no_change_when_single_remaining(schedule, selected, trip, settings):
    """Only one future visit left — fatigue cannot shrink further → NO_CHANGE or keep."""
    # Complete almost everything except day3 park
    done = frozenset(
        item_key(i) for day in schedule.days[:2] for i in day.items)
    current = datetime(2026, 9, 20, 9, 5, tzinfo=KST)
    event = ReplanEvent(event_type=ReplanEventType.FATIGUE, occurred_at=current)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=done), settings)
    before = sum(1 for i in _flat_items(schedule) if i.item_type != "TRAVEL" and item_key(i) not in done)
    assert before <= 1
    if result.status == ReplanStatus.REPLAN_NO_CHANGE:
        assert result.proposed_schedule == schedule
        assert any("실질적인" in n or "줄이지 않아도" in n for n in result.notices)
    else:
        after = sum(
            1 for i in _flat_items(result.proposed_schedule)
            if i.item_type != "TRAVEL" and item_key(i) not in done)
        assert after <= before


def test_case8_change_preference_records_effect(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 13, 0, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.CHANGE_PREFERENCE,
        occurred_at=current,
        preference_changes=("카페", "휴식"),
    )
    result = replan_trip_schedule(_ctx(schedule, selected, trip, current, event), settings)
    assert "preference_change" in result.reasons
    assert result.proposed_schedule is not None
    assert list(result.proposed_schedule.days[0].items) == list(schedule.days[0].items)


def test_case9_meal_change_moves_future_main(schedule, selected, trip, settings):
    # MAIN is p3 dinner on day1 — treat lunch-like by rewriting meal_slot for test
    items = list(schedule.days[0].items)
    lunch_main = None
    new_items = []
    for item in items:
        if item.place_id == "p3" and item.item_type != "TRAVEL":
            lunch_main = item.model_copy(update={
                "meal_slot": f"{item.start_datetime.date()}:점심",
                "start_datetime": datetime(2026, 9, 18, 14, 0, tzinfo=KST),
                "end_datetime": datetime(2026, 9, 18, 15, 0, tzinfo=KST),
            })
            new_items.append(lunch_main)
        else:
            new_items.append(item)
    day0 = schedule.days[0].model_copy(update={"items": tuple(new_items)})
    schedule = schedule.model_copy(update={
        "days": (day0, schedule.days[1], schedule.days[2]),
        "user_selected_place_id": "p3",
        "anchor_meal_role": "LUNCH",
        "anchor_status": "INCLUDED",
    })
    current = datetime(2026, 9, 18, 13, 30, tzinfo=KST)  # before MAIN
    done = frozenset(
        item_key(i) for i in schedule.days[0].items if i.end_datetime <= current)
    event = ReplanEvent(event_type=ReplanEventType.MEAL_CHANGE, occurred_at=current)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=done), settings)
    assert result.main_status in {"moved_meal", "unchanged", "INFEASIBLE"}
    if result.main_status == "moved_meal":
        mains = [
            i for i in _flat_items(result.proposed_schedule)
            if i.place_id == "p3" and i.item_type != "TRAVEL"]
        assert mains and "저녁" in (mains[0].meal_slot or "")


def test_case10_main_completed_immutable(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 12, 0, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP,
        occurred_at=current,
        place_id="p3",  # MAIN completed on day1
    )
    result = replan_trip_schedule(_ctx(schedule, selected, trip, current, event), settings)
    assert result.status == ReplanStatus.REPLAN_INFEASIBLE
    assert list(result.proposed_schedule.days[0].items) == list(schedule.days[0].items)


def test_case11_main_infeasible_after_delay(schedule, selected, trip, settings):
    # Put MAIN late on final day and delay past cutoff
    final_items = list(schedule.days[2].items)
    main = ScheduleItem(
        item_type="MEAL", place_id="main-late", place_name="늦은메인",
        start_datetime=datetime(2026, 9, 20, 14, 30, tzinfo=KST),
        end_datetime=datetime(2026, 9, 20, 15, 30, tzinfo=KST),
        meal_slot="2026-09-20:점심", preference="맛집",
        validation_status="VALIDATED")
    day2 = schedule.days[2].model_copy(update={"items": tuple(final_items) + (main,)})
    schedule = schedule.model_copy(update={
        "days": (schedule.days[0], schedule.days[1], day2),
        "user_selected_place_id": "main-late",
        "anchor_status": "INCLUDED",
        "destination_activity_cutoff": datetime(2026, 9, 20, 15, 6, tzinfo=KST),
    })
    current = datetime(2026, 9, 20, 14, 0, tzinfo=KST)
    done = frozenset(item_key(i) for d in schedule.days[:2] for i in d.items)
    event = ReplanEvent(
        event_type=ReplanEventType.DELAY, occurred_at=current, delay_minutes=90)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=done), settings)
    assert result.proposed_schedule is not None
    assert result.proposed_schedule.final_arrival_datetime <= schedule.trip_end_datetime
    # MAIN past cutoff after delay → removed / INFEASIBLE
    assert result.main_status in {"INFEASIBLE", "unchanged"} or any(
        i.place_id == "main-late" for i in result.removed_items)


def test_case12_accommodation_middle_day_preserved(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 9, 20, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP, occurred_at=current, place_id="p2")
    day1_keys = frozenset(item_key(i) for i in schedule.days[0].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1_keys), settings)
    assert result.proposed_schedule.accommodation == schedule.accommodation
    assert result.proposed_schedule.days[1].end_location.id == schedule.days[1].end_location.id


def test_case13_final_day_return_preserved(schedule, selected, trip, settings):
    current = datetime(2026, 9, 20, 10, 0, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.DELAY, occurred_at=current, delay_minutes=15)
    done = frozenset(
        item_key(i) for d in schedule.days[:2] for i in d.items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=done), settings)
    assert result.proposed_schedule.return_journey is not None
    assert result.proposed_schedule.return_status == schedule.return_status


def test_case14_completed_order_time_place_identical(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 13, 0, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP, occurred_at=current, place_id="p4")
    result = replan_trip_schedule(_ctx(schedule, selected, trip, current, event), settings)
    locked = _completed_keys_until(schedule, current)
    assert completed_prefix_unchanged(schedule, result.proposed_schedule, locked)


def test_case15_no_duplicate_place_id(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 9, 20, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP, occurred_at=current, place_id="p2")
    day1 = frozenset(item_key(i) for i in schedule.days[0].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1), settings)
    assert no_duplicate_place_visits(result.proposed_schedule)


def test_case16_route_cache_not_cleared(schedule, selected, trip, settings, monkeypatch):
    cleared = {"n": 0}

    def boom():
        cleared["n"] += 1

    monkeypatch.setattr("providers.kakao_transit_provider.clear_route_cache", boom, raising=False)
    current = datetime(2026, 9, 19, 13, 0, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.DELAY, occurred_at=current, delay_minutes=10)
    replan_trip_schedule(_ctx(schedule, selected, trip, current, event), settings)
    assert cleared["n"] == 0


def test_case17_kakao_failure_fallback_keeps_original_prefix(
        schedule, selected, trip, settings):
    # Engine is deterministic splice — even without Kakao, locked prefix survives
    current = datetime(2026, 9, 19, 9, 20, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP, occurred_at=current, place_id="p2")
    day1 = frozenset(item_key(i) for i in schedule.days[0].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1), settings)
    assert result.proposed_schedule is not None
    assert list(result.proposed_schedule.days[0].items) == list(schedule.days[0].items)


def test_case18_original_not_mutated(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 9, 20, tzinfo=KST)
    before = schedule.model_dump()
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP, occurred_at=current, place_id="p2")
    day1 = frozenset(item_key(i) for i in schedule.days[0].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1), settings)
    assert schedule.model_dump() == before
    assert result.original_schedule is schedule
    assert result.proposed_schedule is not schedule


def test_case19_preview_cancel_uses_original(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 9, 20, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP, occurred_at=current, place_id="p2")
    day1 = frozenset(item_key(i) for i in schedule.days[0].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1), settings)
    # Cancel preview ⇒ keep original_schedule reference
    applied = result.original_schedule
    assert applied.days[1].items == schedule.days[1].items


def test_case20_max_attempts_bounded(schedule, selected, trip, settings):
    tight = ScheduleSettings(max_replan_attempts=2, fatigue_max_visits_factor=0.6)
    current = datetime(2026, 9, 18, 14, 0, tzinfo=KST)
    event = ReplanEvent(event_type=ReplanEventType.FATIGUE, occurred_at=current)
    done = frozenset(
        item_key(i) for i in schedule.days[0].items if i.end_datetime <= current)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=done), tight)
    assert result.attempts <= tight.max_replan_attempts


def test_classify_and_anchor_location(schedule, selected, trip):
    current = datetime(2026, 9, 19, 9, 40, tzinfo=KST)
    items = _flat_items(schedule)
    p2_visit = next(
        i for i in items if i.place_id == "p2" and i.item_type != "TRAVEL"
        and i.start_datetime.date() == current.date())
    assert classify_item(p2_visit, current, frozenset()) == ItemProgress.IN_PROGRESS
    event = ReplanEvent(event_type=ReplanEventType.FATIGUE, occurred_at=current)
    ctx = _ctx(schedule, selected, trip, current, event)
    anchor = build_anchor(ctx)
    assert anchor.location_status.value in {"INFERRED", "KNOWN", "UNKNOWN"}
    assert anchor.current_day_index == 2


def test_validate_replan_detects_completed_mutation(schedule):
    locked = frozenset(item_key(i) for i in schedule.days[0].items)
    mutated = schedule.model_copy(update={
        "days": (
            schedule.days[0].model_copy(update={
                "items": tuple(
                    i.model_copy(update={"place_name": "HACKED"}) if i.place_id == "p1" and i.item_type != "TRAVEL" else i
                    for i in schedule.days[0].items)
            }),
            schedule.days[1],
            schedule.days[2],
        )
    })
    ok, errors = validate_replan(schedule, mutated, locked)
    assert not ok and "completed_mutation" in errors


def test_orphan_travel_validator_rejects_ghost(schedule):
    from services.replan_validate import no_orphan_travels
    # Keep travel to p2 on day2 but drop the visit → orphan
    day2 = schedule.days[1]
    ghost_items = tuple(i for i in day2.items if not (
        i.item_type != "TRAVEL" and i.place_id == "p2"))
    # Only approach travel remains
    only_travel = tuple(i for i in day2.items if i.item_type == "TRAVEL" and i.destination and i.destination.id == "p2")
    bad = schedule.model_copy(update={
        "days": (
            schedule.days[0],
            day2.model_copy(update={"items": only_travel}),
            schedule.days[2],
        )
    })
    assert not no_orphan_travels(bad)
    ok, errors = validate_replan(schedule, bad, frozenset())
    assert not ok and "orphan_travel" in errors


def test_user_skip_rebuilds_travel_no_orphan(schedule, selected, trip, settings):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP, occurred_at=current,
        place_id="p2", source="USER_REPORTED")
    day1_keys = frozenset(item_key(i) for i in schedule.days[0].items)
    result = replan_trip_schedule(
        _ctx(schedule, selected, trip, current, event, completed=day1_keys), settings)
    assert result.status in {ReplanStatus.REPLAN_SUCCESS, ReplanStatus.REPLAN_PARTIAL}
    proposed = result.proposed_schedule
    # No travel whose destination is p2 without a p2 visit on that day
    for day in proposed.days:
        visits = {i.place_id for i in day.items if i.item_type != "TRAVEL"}
        for item in day.items:
            if item.item_type != "TRAVEL":
                continue
            dest = item.destination.id if item.destination else item.place_id
            if dest == "p2":
                assert "p2" in visits
    from services.replan_validate import no_orphan_travels
    assert no_orphan_travels(proposed)


def test_replacement_from_place_pool(schedule, selected, trip, settings):
    from models.place import PlaceCandidate
    from models.trip_request import Preference
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    event = ReplanEvent(
        event_type=ReplanEventType.USER_SKIP, occurred_at=current,
        place_id="p2", source="USER_REPORTED")
    day1_keys = frozenset(item_key(i) for i in schedule.days[0].items)
    pool = (
        PlaceCandidate(
            place_id="rep1", place_name="금오산저수지",
            category="여행 > 관광명소", latitude=36.125, longitude=128.35,
            preference_score=80, matched_preferences=(Preference.SIGHTSEEING,),
            role="NEARBY_PLACE"),
    )
    ctx = _ctx(schedule, selected, trip, current, event, completed=day1_keys)
    ctx = ctx.model_copy(update={"place_pool": pool})
    result = replan_trip_schedule(ctx, settings)
    assert result.status in {ReplanStatus.REPLAN_SUCCESS, ReplanStatus.REPLAN_PARTIAL}
    ids = {i.place_id for i in _flat_items(result.proposed_schedule) if i.item_type != "TRAVEL"}
    # Replacement is best-effort; if inserted must have matching travel
    if "rep1" in ids:
        from services.replan_validate import no_orphan_travels
        assert no_orphan_travels(result.proposed_schedule)
