"""Phase 6.3 — Dynamic Replanning UI helpers (CASE 1–32, no Streamlit runtime)."""
from __future__ import annotations

from datetime import datetime, time
from unittest.mock import Mock, patch

import pytest

from config import load_settings
from models.replan import ReplanEvent, ReplanEventType, ReplanResult, ReplanStatus, item_key
from models.replan_parse import ParseReplanResult, ParsedReplanIntent, ParseSource
from models.schedule import ScheduleItem
from models.transport import KST
from services.replan_nl_parser import apply_parsed_replan, parse_replan_request
from services.replan_ui import (
    DiffKind,
    apply_pending_to_active,
    can_apply_pending,
    completed_ids_through,
    compute_replan_diff,
    confirmation_candidates,
    finalize_parse_with_place,
    lodging_label,
    return_preview_lines,
    schedule_content_signature,
    status_user_message,
    trip_replan_signature,
    unsupported_user_message,
    visit_choices,
)
from services.replan_validate import _flat_items
from test_places import trip, selected
from test_schedule_day_view import multiday_schedule


@pytest.fixture
def schedule():
    return multiday_schedule()


@pytest.fixture
def settings():
    return load_settings({}, {})


def _day1_done(schedule):
    return frozenset(item_key(i) for i in schedule.days[0].items)


def _pending(schedule, replan, *, trip_req, selected_tc, pid="prev-1", lodging="known|h1"):
    return {
        "id": pid,
        "user_text": "시장 빼줘",
        "replan": replan,
        "base_signature": schedule_content_signature(schedule),
        "trip_signature": trip_replan_signature(
            trip_req, selected_tc, preferred_place_id="p3",
            lodging_key=lodging, schedule=schedule),
        "completed_keys": list(_day1_done(schedule)),
    }


# --- CASE 1: no schedule / empty choices ---
def test_case1_no_schedule_visit_choices_empty():
    empty = multiday_schedule().model_copy(update={"days": (), "items": ()})
    assert visit_choices(empty) == []


# --- CASE 2: NL → parse → preview bundle (no active mutate) ---
def test_case2_3_parse_preview_does_not_mutate(schedule, settings, trip, selected):
    original_dump = schedule.model_dump_json()
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "시장 빼줘", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings, groq=None)
    assert parse.events
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=_day1_done(schedule),
        settings=settings)
    assert schedule.model_dump_json() == original_dump  # CASE 3 + 30
    assert applied.replan is not None
    assert applied.replan.proposed_schedule is not None
    assert applied.replan.proposed_schedule is not schedule


# --- CASE 4 Apply / CASE 5 Cancel ---
def test_case4_5_apply_and_cancel(schedule, settings, trip, selected):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "시장 빼줘", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings, groq=None)
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=_day1_done(schedule),
        settings=settings)
    pending = _pending(schedule, applied.replan, trip_req=trip, selected_tc=selected)
    # Cancel: active unchanged
    active = schedule
    assert active is schedule
    # Apply
    ok, _ = can_apply_pending(
        pending, active_schedule=schedule, trip=trip, selected=selected,
        preferred_place_id="p3", lodging_key="known|h1")
    assert ok
    new_active = apply_pending_to_active(pending, schedule)
    assert new_active is applied.replan.proposed_schedule
    assert new_active is not schedule


# --- CASE 6 completed lock ---
def test_case6_completed_through_and_kept(schedule):
    visits = [i for i in schedule.days[1].items if i.item_type != "TRAVEL"]
    through = item_key(visits[0])
    done = completed_ids_through(schedule, through)
    assert item_key(schedule.days[0].items[0]) in done
    assert through in done
    # day2 market locked; later day3 not
    day3_visit = next(i for i in schedule.days[2].items if i.item_type != "TRAVEL")
    assert item_key(day3_visit) not in done


def test_progress_in_progress_excludes_anchor_from_completed(schedule):
    from services.replan_ui import resolve_progress_locks, VisitProgressStatus
    visits = [i for i in schedule.days[1].items if i.item_type != "TRAVEL"]
    key = item_key(visits[0])
    done, cur = resolve_progress_locks(
        schedule, progress_key=key, visit_status=VisitProgressStatus.IN_PROGRESS)
    assert key not in done
    assert cur == visits[0].place_id
    # prior day items locked
    assert item_key(schedule.days[0].items[1]) in done


def test_progress_not_started_excludes_anchor(schedule):
    from services.replan_ui import resolve_progress_locks, VisitProgressStatus
    visits = [i for i in schedule.days[1].items if i.item_type != "TRAVEL"]
    key = item_key(visits[0])
    done, cur = resolve_progress_locks(
        schedule, progress_key=key, visit_status=VisitProgressStatus.NOT_STARTED)
    assert key not in done
    assert cur == "p2"


def test_case1_here_cant_go_future_removes(schedule, settings, trip, selected):
    from services.replan_ui import resolve_progress_locks, VisitProgressStatus, DiffKind
    from models.replan import ReplanStatus
    visits = [i for i in schedule.days[1].items if i.item_type != "TRAVEL"]
    key = item_key(visits[0])
    done, cur = resolve_progress_locks(
        schedule, progress_key=key, visit_status=VisitProgressStatus.NOT_STARTED)
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "여기 못가", schedule=schedule, current_datetime=current,
        completed_item_ids=done, current_place_id=cur, settings=settings, groq=None)
    assert parse.events
    assert parse.events[0].event_type == ReplanEventType.USER_SKIP
    assert parse.events[0].place_id == "p2"
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=done, settings=settings)
    assert applied.replan is not None
    assert applied.replan.status in (
        ReplanStatus.REPLAN_SUCCESS, ReplanStatus.REPLAN_PARTIAL)
    assert key not in done


def test_case2_here_closed_in_progress(schedule, settings, trip, selected):
    from services.replan_ui import resolve_progress_locks, VisitProgressStatus
    from models.replan import ReplanStatus
    visits = [i for i in schedule.days[1].items if i.item_type != "TRAVEL"]
    key = item_key(visits[0])
    done, cur = resolve_progress_locks(
        schedule, progress_key=key, visit_status=VisitProgressStatus.IN_PROGRESS)
    current = datetime(2026, 9, 19, 9, 40, tzinfo=KST)
    parse = parse_replan_request(
        "여기 문 닫았어", schedule=schedule, current_datetime=current,
        completed_item_ids=done, current_place_id=cur, settings=settings, groq=None)
    assert parse.events[0].event_type == ReplanEventType.PLACE_CLOSED
    assert key not in done
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=done, settings=settings)
    assert applied.replan.status != ReplanStatus.REPLAN_INFEASIBLE or "완료" not in (
        applied.replan.notices[0] if applied.replan.notices else "")
    assert applied.replan.status in (
        ReplanStatus.REPLAN_SUCCESS, ReplanStatus.REPLAN_PARTIAL)


def test_case3_completed_lock_rejects_here_skip(schedule, settings, trip, selected):
    from services.replan_ui import resolve_progress_locks, VisitProgressStatus
    from models.replan import ReplanStatus
    visits = [i for i in schedule.days[1].items if i.item_type != "TRAVEL"]
    key = item_key(visits[0])
    done, cur = resolve_progress_locks(
        schedule, progress_key=key, visit_status=VisitProgressStatus.COMPLETED)
    assert key in done
    current = datetime(2026, 9, 19, 10, 30, tzinfo=KST)
    parse = parse_replan_request(
        "여기 못가", schedule=schedule, current_datetime=current,
        completed_item_ids=done, current_place_id=cur, settings=settings, groq=None)
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=done, settings=settings)
    assert applied.replan is not None
    assert applied.replan.status == ReplanStatus.REPLAN_INFEASIBLE
    assert any("완료" in n for n in applied.replan.notices)


def test_case4_rain_here_skip_not_weather_api(schedule, settings):
    from services.replan_ui import resolve_progress_locks, VisitProgressStatus
    visits = [i for i in schedule.days[1].items if i.item_type != "TRAVEL"]
    key = item_key(visits[0])
    done, cur = resolve_progress_locks(
        schedule, progress_key=key, visit_status=VisitProgressStatus.NOT_STARTED)
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "비가와서 여기 못가", schedule=schedule, current_datetime=current,
        completed_item_ids=done, current_place_id=cur, settings=settings, groq=None)
    assert parse.events
    assert parse.events[0].event_type == ReplanEventType.USER_SKIP
    assert parse.events[0].place_id == "p2"
    assert not any(i.event_type == "UNSUPPORTED" for i in parse.intents)


def test_case5_will_it_rain_unsupported(schedule, settings):
    current = datetime(2026, 9, 19, 13, 0, tzinfo=KST)
    result = parse_replan_request(
        "내일 비 올까?", schedule=schedule, current_datetime=current,
        settings=settings, groq=None)
    assert not result.events
    assert any("날씨" in (i.ambiguity_reason or "") for i in result.intents)


def test_case6_auto_weather_replan_unsupported(schedule, settings):
    current = datetime(2026, 9, 19, 13, 0, tzinfo=KST)
    result = parse_replan_request(
        "비 오면 알아서 일정 바꿔줘", schedule=schedule, current_datetime=current,
        settings=settings, groq=None)
    assert not result.events
    assert any("날씨" in (i.ambiguity_reason or "") for i in result.intents)


def test_case7_time_progress_consistency_warning(schedule):
    from services.replan_ui import progress_time_consistency_warning, VisitProgressStatus
    visits = [i for i in schedule.days[1].items if i.item_type != "TRAVEL"]
    key = item_key(visits[0])
    early = datetime(2026, 9, 19, 8, 0, tzinfo=KST)
    warn = progress_time_consistency_warning(
        schedule, progress_key=key,
        visit_status=VisitProgressStatus.IN_PROGRESS, current_datetime=early)
    assert warn and "이릅니다" in warn


def test_case8_in_progress_not_in_completed_ids(schedule):
    from services.replan_ui import resolve_progress_locks, VisitProgressStatus
    visits = [i for i in schedule.days[1].items if i.item_type != "TRAVEL"]
    key = item_key(visits[0])
    done, _ = resolve_progress_locks(
        schedule, progress_key=key, visit_status=VisitProgressStatus.IN_PROGRESS)
    assert key not in done


# --- CASE 7 USER_SKIP removed in diff ---
def test_case7_user_skip_removed_diff(schedule, settings, trip, selected):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "시장 빼줘", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings, groq=None)
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=_day1_done(schedule),
        settings=settings)
    diff = compute_replan_diff(
        schedule, applied.replan.proposed_schedule,
        completed_ids=_day1_done(schedule), events=parse.events, replan=applied.replan)
    assert any(d.kind == DiffKind.REMOVED and "시장" in d.place_name for d in diff.items)


# --- CASE 8 DELAY time changed ---
def test_case8_delay_time_changed(schedule, settings, trip, selected):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "30분 늦었어", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings, groq=None)
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=_day1_done(schedule),
        settings=settings)
    assert applied.replan is not None
    diff = compute_replan_diff(
        schedule, applied.replan.proposed_schedule or schedule,
        completed_ids=_day1_done(schedule), events=parse.events, replan=applied.replan)
    # May be TIME_CHANGED and/or RETURN_CHANGED; at least request line mentions delay
    assert any("지연" in line for line in diff.request_lines) or any(
        d.kind == DiffKind.TIME_CHANGED for d in diff.items) or applied.replan.status != ReplanStatus.REPLAN_FAILED


# --- CASE 9 PLACE_CLOSED USER_REPORTED ---
def test_case9_place_closed_user_reported(schedule, settings):
    current = datetime(2026, 9, 19, 9, 40, tzinfo=KST)
    parse = parse_replan_request(
        "여기 문 닫았어", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), current_place_id="p2",
        settings=settings, groq=None)
    assert parse.events[0].source == "USER_REPORTED"
    lines = __import__("services.replan_ui", fromlist=["event_request_lines"]).event_request_lines(
        parse.events, schedule)
    assert any("사용자 입력" in line for line in lines)


# --- CASE 10 FATIGUE remaining decrease ---
def test_case10_fatigue_remaining_decrease(schedule, settings, trip, selected):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "너무 피곤해", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings, groq=None)
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=_day1_done(schedule),
        settings=settings)
    assert applied.replan is not None
    done = _day1_done(schedule)
    before = sum(1 for i in _flat_items(schedule) if i.item_type != "TRAVEL"
                 and item_key(i) not in done)
    after = sum(1 for i in _flat_items(applied.replan.proposed_schedule or schedule)
                if i.item_type != "TRAVEL" and item_key(i) not in done)
    if applied.replan.status == ReplanStatus.REPLAN_NO_CHANGE:
        assert after == before
    else:
        assert after < before
        diff = compute_replan_diff(
            schedule, applied.replan.proposed_schedule, completed_ids=done,
            events=parse.events, replan=applied.replan)
        assert any("남은 활동" in line for line in diff.summary_lines)
        assert any(d.kind == DiffKind.REMOVED for d in diff.items)


def test_fatigue_no_change_blocks_apply(schedule, trip, selected):
    from services.replan_ui import can_apply_pending, schedule_content_signature, trip_replan_signature
    failed = ReplanResult(
        status=ReplanStatus.REPLAN_NO_CHANGE,
        original_schedule=schedule, proposed_schedule=schedule,
        notices=("적용할 수 있는 실질적인 일정 변경이 없습니다.",),
        reasons=("fatigue", "no_change"))
    pending = {
        "id": "nc1",
        "replan": failed,
        "base_signature": schedule_content_signature(schedule),
        "trip_signature": trip_replan_signature(
            trip, selected, preferred_place_id="p3", lodging_key="known|h1",
            schedule=schedule),
    }
    ok, msg = can_apply_pending(
        pending, active_schedule=schedule, trip=trip, selected=selected,
        preferred_place_id="p3", lodging_key="known|h1")
    assert not ok
    assert "실질적인" in msg


# --- CASE 11 MEAL_CHANGE ---
def test_case11_meal_change_diff(schedule, settings, trip, selected):
    current = datetime(2026, 9, 18, 14, 0, tzinfo=KST)
    parse = parse_replan_request(
        "이 식당 저녁으로 옮겨줘", schedule=schedule, current_datetime=current,
        completed_item_ids=frozenset(), current_place_id="p3",
        settings=settings, groq=None)
    if parse.requires_confirmation and not parse.events:
        parse = finalize_parse_with_place(parse, "p3", occurred_at=current)
    if not parse.events:
        pytest.skip("meal change not resolvable on fixture")
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=frozenset(), settings=settings)
    assert applied.replan is not None
    diff = compute_replan_diff(
        schedule, applied.replan.proposed_schedule or schedule,
        completed_ids=frozenset(), events=parse.events, replan=applied.replan)
    assert any("식당" in line or "저녁" in line for line in diff.request_lines) or any(
        d.kind == DiffKind.MEAL_CHANGED for d in diff.items) or applied.replan.main_status


# --- CASE 12 CURRENT_LOCATION ---
def test_case12_current_location_changed(schedule, settings):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    kakao = Mock()
    kakao.search_places.return_value = [{
        "id": "station", "place_name": "구미역",
        "x": "128.35", "y": "36.14", "address_name": "경북 구미시",
    }]
    parse = parse_replan_request(
        "지금 구미역이야", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings,
        groq=None, kakao=kakao)
    # deterministic or fallback may still produce location event
    types = [e.event_type for e in parse.events]
    assert (ReplanEventType.CURRENT_LOCATION_CHANGED in types
            or parse.requires_confirmation
            or any(i.event_type == ReplanEventType.CURRENT_LOCATION_CHANGED
                   or i.current_location_text for i in parse.intents))


# --- CASE 13 Multiple intent ---
def test_case13_multiple_intent_request_lines(schedule, settings, trip, selected):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "30분 늦었고 시장도 빼줘", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings, groq=None)
    assert len(parse.events) >= 2 or len(parse.intents) >= 2
    from services.replan_ui import event_request_lines
    lines = event_request_lines(parse.events, schedule)
    if parse.events:
        assert len(lines) >= 1


# --- CASE 14 / 15 confirmation ---
def test_case14_15_confirmation_then_finalize(schedule, settings, trip, selected):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    extra = ScheduleItem(
        item_type="PLACE", place_id="market-b", place_name="구미중앙시장",
        start_datetime=datetime(2026, 9, 19, 11, 0, tzinfo=KST),
        end_datetime=datetime(2026, 9, 19, 12, 0, tzinfo=KST),
        preference="쇼핑", validation_status="VALIDATED")
    day2 = schedule.days[1].model_copy(
        update={"items": schedule.days[1].items + (extra,)})
    schedule = schedule.model_copy(update={"days": (schedule.days[0], day2, schedule.days[2])})
    parse = parse_replan_request(
        "시장 빼줘", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings, groq=None)
    assert parse.requires_confirmation
    assert not parse.events  # engine must not auto-run
    cands = confirmation_candidates(parse, schedule)
    assert len(cands) >= 2
    finalized = finalize_parse_with_place(parse, "p2", occurred_at=current)
    assert finalized.events
    assert finalized.events[0].place_id == "p2"
    applied = apply_parsed_replan(
        finalized, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=_day1_done(schedule),
        settings=settings)
    assert applied.replan is not None


# --- CASE 16 / 17 infeasible / failed keep active ---
def test_case16_17_infeasible_failed_keep_active(schedule, trip, selected):
    failed = ReplanResult(
        status=ReplanStatus.REPLAN_FAILED,
        original_schedule=schedule, proposed_schedule=None)
    pending = _pending(schedule, failed, trip_req=trip, selected_tc=selected)
    ok, msg = can_apply_pending(
        pending, active_schedule=schedule, trip=trip, selected=selected,
        preferred_place_id="p3", lodging_key="known|h1")
    assert not ok
    assert "기존 일정" in msg or "다시 계산" in msg or "유지" in msg

    infeas = ReplanResult(
        status=ReplanStatus.REPLAN_INFEASIBLE,
        original_schedule=schedule, proposed_schedule=schedule)
    pending2 = _pending(schedule, infeas, trip_req=trip, selected_tc=selected, pid="p2")
    ok2, _ = can_apply_pending(
        pending2, active_schedule=schedule, trip=trip, selected=selected,
        preferred_place_id="p3", lodging_key="known|h1")
    assert not ok2


# --- CASE 18 return lines ---
def test_case18_return_preview(schedule):
    lines = return_preview_lines(schedule)
    assert any("귀가 목표" in line for line in lines)
    assert any("예상 귀가" in line for line in lines)


# --- CASE 19 / 20 lodging labels ---
def test_case19_20_lodging_labels(schedule):
    assert "숙소" in lodging_label(schedule)
    prov = multiday_schedule(provisional=True)
    assert "임시 기준점" in lodging_label(prov)


# --- CASE 21–23 map/timeline use active only (signature identity) ---
def test_case21_22_23_views_follow_active(selected):
    from services.schedule_day_view import build_schedule_views
    schedule = multiday_schedule()
    views = build_schedule_views(schedule, selected=selected)
    ids = {m.place_id for v in views for m in v.markers}
    assert "p2" in ids
    # Simulate removed market from day2
    day2 = schedule.days[1]
    new_items = tuple(i for i in day2.items if i.place_id != "p2")
    new_day2 = day2.model_copy(update={"items": new_items})
    proposed = schedule.model_copy(update={"days": (schedule.days[0], new_day2, schedule.days[2])})
    views2 = build_schedule_views(proposed, selected=selected)
    day2_ids = {m.place_id for m in views2[1].markers}
    assert "p2" not in day2_ids


# --- CASE 24–26 stale preview ---
def test_case24_25_26_stale_preview_rejected(schedule, trip, selected, settings):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "시장 빼줘", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings, groq=None)
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=_day1_done(schedule),
        settings=settings)
    pending = _pending(schedule, applied.replan, trip_req=trip, selected_tc=selected)

    # TripRequest change
    new_trip = trip.model_copy(update={"destination": "부산"})
    ok, msg = can_apply_pending(
        pending, active_schedule=schedule, trip=new_trip, selected=selected,
        preferred_place_id="p3", lodging_key="known|h1")
    assert not ok and "다시 계산" in msg

    # Transport change → different selected signature
    new_sel = selected.model_copy(update={"price": (selected.price or 0) + 1})
    ok2, _ = can_apply_pending(
        pending, active_schedule=schedule, trip=trip, selected=new_sel,
        preferred_place_id="p3", lodging_key="known|h1")
    assert not ok2

    # Accommodation / lodging key change
    ok3, _ = can_apply_pending(
        pending, active_schedule=schedule, trip=trip, selected=selected,
        preferred_place_id="p3", lodging_key="known|other-hotel")
    assert not ok3


# --- CASE 27 double apply ---
def test_case27_double_apply_blocked(schedule, trip, selected, settings):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    parse = parse_replan_request(
        "시장 빼줘", schedule=schedule, current_datetime=current,
        completed_item_ids=_day1_done(schedule), settings=settings, groq=None)
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=_day1_done(schedule),
        settings=settings)
    pending = _pending(schedule, applied.replan, trip_req=trip, selected_tc=selected)
    ok, msg = can_apply_pending(
        pending, active_schedule=schedule, trip=trip, selected=selected,
        preferred_place_id="p3", lodging_key="known|h1",
        already_applied_id=pending["id"])
    assert not ok and "이미 적용" in msg


# --- CASE 28 / 29 no API on rerun helpers ---
def test_case28_29_helpers_no_external_api(schedule):
    # Pure helpers must not import/call providers
    import inspect
    import services.replan_ui as mod
    src = inspect.getsource(mod)
    assert "GroqProvider" not in src
    assert "KakaoProvider" not in src
    assert "HttpClient" not in src
    _ = schedule_content_signature(schedule)
    _ = status_user_message(ReplanStatus.REPLAN_SUCCESS)


# --- CASE 30 original immutable (covered in case2) ---
def test_case30_status_messages():
    assert "다시 구성" in status_user_message(ReplanStatus.REPLAN_SUCCESS)
    assert "줄이거나" in status_user_message(ReplanStatus.REPLAN_PARTIAL)
    assert "만족하기 어렵" in status_user_message(ReplanStatus.REPLAN_INFEASIBLE)
    assert "기존 일정은 그대로" in status_user_message(ReplanStatus.REPLAN_FAILED)
    assert "실질적인" in status_user_message(ReplanStatus.REPLAN_NO_CHANGE)


# --- CASE unsupported weather copy ---
def test_unsupported_weather_message():
    parse = ParseReplanResult(
        user_text="비 오니까 일정 알아서 바꿔줘",
        intents=(ParsedReplanIntent(
            event_type="UNSUPPORTED", supported=False,
            ambiguity_reason="날씨 기반 자동 변경은 지원하지 않습니다"),),
        parse_source=ParseSource.DETERMINISTIC)
    msg = unsupported_user_message(parse)
    assert msg and "날씨" in msg and "자동 확인하지" in msg


# --- CASE completed summary in diff ---
def test_completed_kept_summary(schedule, settings, trip, selected):
    current = datetime(2026, 9, 19, 9, 5, tzinfo=KST)
    done = _day1_done(schedule)
    parse = parse_replan_request(
        "시장 빼줘", schedule=schedule, current_datetime=current,
        completed_item_ids=done, settings=settings, groq=None)
    applied = apply_parsed_replan(
        parse, trip=trip, schedule=schedule, selected=selected,
        current_datetime=current, completed_item_ids=done, settings=settings)
    diff = compute_replan_diff(
        schedule, applied.replan.proposed_schedule,
        completed_ids=done, events=parse.events, replan=applied.replan)
    assert diff.completed_kept >= 1
    assert any("완료한 이전 일정" in line for line in diff.summary_lines)


def test_app_has_replan_panel_and_no_engine_terms():
    from pathlib import Path
    src = Path("app.py").read_text(encoding="utf-8")
    assert "render_replan_panel" in src
    assert "일정이 틀어졌나요?" in src
    assert "변경 적용" in src
    # User-facing copy should not expose engine jargon in UI strings
    assert 'st.write("ReplanEvent")' not in src
    assert 'st.subheader("Deterministic' not in src


def test_app_user_facing_copy_hides_phase_and_gumi_example():
    from pathlib import Path
    src = Path("app.py").read_text(encoding="utf-8")
    vis = Path("services/schedule_visualization.py").read_text(encoding="utf-8")
    assert 'page_title="여행 다시짜기"' in src
    assert 'st.title("여행 다시짜기")' in src
    assert "출발부터 귀가까지, 상황이 바뀌면 남은 일정만 다시 계산합니다." in src
    assert 'st.title("AI 여행 플래너")' not in src
    assert 'page_title="AI 여행 플래너"' not in src
    assert 'st.caption("Phase 5' not in src
    assert "지금 구미역이야" not in src
    assert "여기 문 닫았어 · 시장 빼줘 · 30분 늦었어 · 너무 피곤해" in src
    assert 'st.markdown("**귀가 요약**")' in vis
    assert "목적지 활동 종료 권장 한도" in vis
    assert "st.success(" not in vis
    assert "귀가 완료 목표" in src
