"""Phase 6.3 — Replan preview helpers (diff, signature, copy). No Streamlit import."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum

from models.replan import ReplanEvent, ReplanResult, ReplanStatus, item_key
from models.replan_parse import ParseReplanResult
from models.schedule import TripSchedule
from models.transport import TransportCandidate
from models.trip_request import TripRequest
from services.replan_service import classify_item
from services.replan_validate import _flat_items
from models.replan import ItemProgress


class DiffKind(str, Enum):
    REMOVED = "REMOVED"
    ADDED = "ADDED"
    TIME_CHANGED = "TIME_CHANGED"
    MEAL_CHANGED = "MEAL_CHANGED"
    DAY_CHANGED = "DAY_CHANGED"
    RETURN_CHANGED = "RETURN_CHANGED"
    ACCOMMODATION_CHANGED = "ACCOMMODATION_CHANGED"
    UNCHANGED_COMPLETED = "UNCHANGED_COMPLETED"


class VisitProgressStatus(str, Enum):
    """User-declared status for the selected progress activity (not system GPS)."""
    NOT_STARTED = "NOT_STARTED"  # 방문 전 — FUTURE for lock
    IN_PROGRESS = "IN_PROGRESS"  # 방문 중 — not completed
    COMPLETED = "COMPLETED"  # 방문 완료 — inclusive lock


@dataclass(frozen=True)
class ReplanDiffItem:
    kind: DiffKind
    place_id: str
    place_name: str
    detail: str = ""


@dataclass(frozen=True)
class ReplanDiff:
    items: tuple[ReplanDiffItem, ...]
    completed_kept: int
    request_lines: tuple[str, ...]
    summary_lines: tuple[str, ...]


def schedule_content_signature(schedule: TripSchedule) -> str:
    """Stable hash of schedule content for stale-preview checks."""
    payload = schedule.model_dump_json()
    return hashlib.sha256(payload.encode()).hexdigest()


def trip_replan_signature(
        trip: TripRequest,
        selected: TransportCandidate,
        *,
        preferred_place_id: str | None,
        lodging_key: str,
        schedule: TripSchedule | None = None,
) -> str:
    base = (
        trip.model_dump_json()
        + selected.model_dump_json()
        + str(preferred_place_id or "")
        + lodging_key
    )
    if schedule is not None:
        base += schedule_content_signature(schedule)
    return hashlib.sha256(base.encode()).hexdigest()


def visit_choices(schedule: TripSchedule) -> list[tuple[str, str]]:
    """(item_key, label) for non-travel visits in chronological order."""
    rows: list[tuple[str, str]] = []
    for item in _flat_items(schedule):
        if item.item_type == "TRAVEL":
            continue
        label = f"{item.start_datetime:%m/%d %H:%M} · {item.place_name}"
        rows.append((item_key(item), label))
    return rows


def completed_ids_through(schedule: TripSchedule, through_key: str | None) -> frozenset[str]:
    """Lock all visits/travels up to and including the selected visit key (LAST_COMPLETED)."""
    if not through_key:
        return frozenset()
    locked: set[str] = set()
    found = False
    for item in _flat_items(schedule):
        locked.add(item_key(item))
        if item_key(item) == through_key:
            found = True
            break
    return frozenset(locked) if found else frozenset()


def completed_ids_before(schedule: TripSchedule, before_key: str | None) -> frozenset[str]:
    """Lock items strictly before ``before_key`` (excludes the anchor itself)."""
    if not before_key:
        return frozenset()
    locked: set[str] = set()
    for item in _flat_items(schedule):
        if item_key(item) == before_key:
            return frozenset(locked)
        locked.add(item_key(item))
    return frozenset()


def resolve_progress_locks(
        schedule: TripSchedule,
        *,
        progress_key: str | None,
        visit_status: VisitProgressStatus | str | None,
) -> tuple[frozenset[str], str | None]:
    """Return (completed_item_ids, current_place_id) from separated progress UI.

    - COMPLETED: inclusive lock through progress_key; current_place_id still set for "여기"
      so Completed Lock can reject skip/closed on that place.
    - IN_PROGRESS / NOT_STARTED: exclusive lock (prior only); current_place_id = that place.
    """
    status = VisitProgressStatus(visit_status) if visit_status else VisitProgressStatus.NOT_STARTED
    if not progress_key:
        return frozenset(), None

    place_id = None
    for item in _flat_items(schedule):
        if item_key(item) == progress_key and item.item_type != "TRAVEL":
            place_id = item.place_id
            break
    if place_id is None:
        return frozenset(), None

    if status == VisitProgressStatus.COMPLETED:
        return completed_ids_through(schedule, progress_key), place_id
    # 방문 전 / 방문 중 → 해당 Activity는 completed_item_ids에 넣지 않음
    return completed_ids_before(schedule, progress_key), place_id


def find_visit_by_key(schedule: TripSchedule, key: str | None):
    if not key:
        return None
    for item in _flat_items(schedule):
        if item_key(item) == key and item.item_type != "TRAVEL":
            return item
    return None


def progress_time_consistency_warning(
        schedule: TripSchedule,
        *,
        progress_key: str | None,
        visit_status: VisitProgressStatus | str | None,
        current_datetime: datetime,
) -> str | None:
    """Warn when declared progress conflicts with the user-set clock (demo-friendly)."""
    item = find_visit_by_key(schedule, progress_key)
    if item is None or visit_status is None:
        return None
    status = VisitProgressStatus(visit_status)
    start, end = item.start_datetime, item.end_datetime
    if status == VisitProgressStatus.IN_PROGRESS:
        if current_datetime < start:
            return (
                f"설정한 현재 시각({current_datetime:%H:%M})이 "
                f"「{item.place_name}」시작({start:%H:%M})보다 이릅니다. "
                f"시각을 조정하거나 상태를 「방문 전」으로 바꿔주세요."
            )
        if current_datetime > end:
            return (
                f"설정한 현재 시각({current_datetime:%H:%M})이 "
                f"「{item.place_name}」종료({end:%H:%M})보다 늦습니다. "
                f"시각을 조정하거나 상태를 「방문 완료」로 바꿔주세요."
            )
    elif status == VisitProgressStatus.NOT_STARTED:
        if current_datetime >= end:
            return (
                f"설정한 현재 시각 기준으로 「{item.place_name}」은 이미 지난 일정입니다. "
                f"상태를 「방문 완료」로 바꾸거나 시각을 앞당겨 주세요."
            )
    elif status == VisitProgressStatus.COMPLETED:
        if current_datetime < start:
            return (
                f"설정한 현재 시각이 「{item.place_name}」시작보다 이른데 방문 완료로 표시되어 있습니다. "
                f"시각을 조정하거나 상태를 바꿔주세요."
            )
    return None


def inferred_current_place_name(
        schedule: TripSchedule,
        current: datetime,
        completed_ids: frozenset[str],
) -> str:
    items = _flat_items(schedule)
    last_done = None
    in_progress = None
    for item in items:
        if item.item_type == "TRAVEL":
            continue
        progress = classify_item(item, current, completed_ids)
        if progress == ItemProgress.COMPLETED:
            last_done = item
        elif progress == ItemProgress.IN_PROGRESS:
            in_progress = item
            break
    if in_progress:
        return in_progress.place_name
    if last_done:
        return last_done.place_name
    return "일정 기준 추정 위치 없음"


def status_user_message(status: ReplanStatus) -> str:
    return {
        ReplanStatus.REPLAN_SUCCESS: "남은 일정을 다시 구성했습니다.",
        ReplanStatus.REPLAN_PARTIAL: "일부 일정을 줄이거나 제외해 가능한 일정으로 조정했습니다.",
        ReplanStatus.REPLAN_INFEASIBLE: "현재 조건으로는 요청한 변경과 기존 제약을 모두 만족하기 어렵습니다.",
        ReplanStatus.REPLAN_FAILED: "일정을 다시 계산하지 못했습니다. 기존 일정은 그대로 유지됩니다.",
        ReplanStatus.REPLAN_NO_CHANGE: "적용할 수 있는 실질적인 일정 변경이 없습니다.",
    }.get(status, "일정 변경 결과를 확인해주세요.")


def event_request_lines(events: tuple[ReplanEvent, ...], schedule: TripSchedule) -> list[str]:
    names = {
        i.place_id: i.place_name
        for i in _flat_items(schedule) if i.item_type != "TRAVEL"
    }
    lines: list[str] = []
    for event in events:
        et = event.event_type.value
        if et == "DELAY":
            lines.append(f"일정 {event.delay_minutes or 0}분 지연")
        elif et == "USER_SKIP":
            lines.append(f"{names.get(event.place_id or '', event.place_id or '장소')} 제외")
        elif et == "PLACE_CLOSED":
            lines.append(
                f"사용자 입력에 따라 {names.get(event.place_id or '', '현재 장소')} 제외")
        elif et == "FATIGUE":
            lines.append("남은 활동 수를 줄이는 요청")
        elif et == "CHANGE_PREFERENCE":
            prefs = ", ".join(event.preference_changes) or "성향 변경"
            lines.append(f"남은 일정 성향: {prefs}")
        elif et == "CURRENT_LOCATION_CHANGED":
            lines.append("현재 위치 변경 후 남은 일정 재계산")
        elif et == "MEAL_CHANGE":
            role = event.target_meal_role or "DINNER"
            label = {"LUNCH": "점심", "DINNER": "저녁"}.get(role, role)
            lines.append(f"기준 식당을 {label} 시간대로 조정")
        else:
            lines.append(et)
    return lines


def compute_replan_diff(
        original: TripSchedule,
        proposed: TripSchedule,
        *,
        completed_ids: frozenset[str],
        events: tuple[ReplanEvent, ...] = (),
        replan: ReplanResult | None = None,
) -> ReplanDiff:
    orig_visits = {
        item_key(i): i for i in _flat_items(original) if i.item_type != "TRAVEL"
    }
    prop_visits = {
        item_key(i): i for i in _flat_items(proposed) if i.item_type != "TRAVEL"
    }
    orig_by_place = {}
    for item in _flat_items(original):
        if item.item_type == "TRAVEL":
            continue
        orig_by_place.setdefault(item.place_id, []).append(item)
    prop_by_place = {}
    for item in _flat_items(proposed):
        if item.item_type == "TRAVEL":
            continue
        prop_by_place.setdefault(item.place_id, []).append(item)

    diffs: list[ReplanDiffItem] = []
    completed_kept = 0
    for key, item in orig_visits.items():
        if key in completed_ids:
            completed_kept += 1
            continue
        # Future/original visit gone from proposed (by item key or by place_id among non-completed)
        if key in prop_visits:
            continue
        still_has_place = any(
            item_key(p) not in completed_ids and p.place_id == item.place_id
            for p in _flat_items(proposed) if p.item_type != "TRAVEL"
        )
        if not still_has_place:
            diffs.append(ReplanDiffItem(
                DiffKind.REMOVED, item.place_id, item.place_name,
                f"{item.start_datetime:%H:%M} 일정에서 제외"))

    for place_id, items in prop_by_place.items():
        if place_id not in orig_by_place:
            for item in items:
                diffs.append(ReplanDiffItem(
                    DiffKind.ADDED, item.place_id, item.place_name,
                    f"{item.start_datetime:%H:%M} 추가"))

    for place_id, prop_items in prop_by_place.items():
        orig_items = orig_by_place.get(place_id) or []
        if not orig_items:
            continue
        # Compare first non-completed pair by place
        o = next((i for i in orig_items if item_key(i) not in completed_ids), orig_items[0])
        p = prop_items[0]
        if o.start_datetime != p.start_datetime or o.end_datetime != p.end_datetime:
            diffs.append(ReplanDiffItem(
                DiffKind.TIME_CHANGED, place_id, p.place_name,
                f"{o.start_datetime:%H:%M} → {p.start_datetime:%H:%M}"))
        o_meal = "점심" if o.meal_slot and "점심" in o.meal_slot else (
            "저녁" if o.meal_slot and "저녁" in o.meal_slot else "")
        p_meal = "점심" if p.meal_slot and "점심" in p.meal_slot else (
            "저녁" if p.meal_slot and "저녁" in p.meal_slot else "")
        if o_meal and p_meal and o_meal != p_meal:
            diffs.append(ReplanDiffItem(
                DiffKind.MEAL_CHANGED, place_id, p.place_name,
                f"{o_meal} → {p_meal}"))

    if original.final_arrival_datetime != proposed.final_arrival_datetime:
        diffs.append(ReplanDiffItem(
            DiffKind.RETURN_CHANGED, "return", "귀가",
            f"{_fmt_dt(original.final_arrival_datetime)} → {_fmt_dt(proposed.final_arrival_datetime)}"))
    if (original.accommodation and proposed.accommodation
            and original.accommodation.id != proposed.accommodation.id):
        diffs.append(ReplanDiffItem(
            DiffKind.ACCOMMODATION_CHANGED,
            proposed.accommodation.id, proposed.accommodation.name, "숙소 기준 변경"))

    # Lodging arrival time (숙소 이동)
    def _lodge_end(sched: TripSchedule) -> datetime | None:
        ends = [
            i.end_datetime for i in _flat_items(sched)
            if i.item_type == "TRAVEL" and i.reason == "숙소 이동"
        ]
        return max(ends) if ends else None

    o_lodge, p_lodge = _lodge_end(original), _lodge_end(proposed)
    if o_lodge and p_lodge and o_lodge != p_lodge:
        diffs.append(ReplanDiffItem(
            DiffKind.ACCOMMODATION_CHANGED, "lodging-arrival", "숙소 도착",
            f"{o_lodge:%H:%M} → {p_lodge:%H:%M}"))

    # Deduplicate similar REMOVED for same place_id
    seen: set[tuple[str, str]] = set()
    unique: list[ReplanDiffItem] = []
    for item in diffs:
        mark = (item.kind.value, item.place_id)
        if mark in seen and item.kind == DiffKind.REMOVED:
            continue
        seen.add(mark)
        unique.append(item)

    before_n = sum(
        1 for i in _flat_items(original)
        if i.item_type != "TRAVEL" and item_key(i) not in completed_ids)
    after_n = sum(
        1 for i in _flat_items(proposed)
        if i.item_type != "TRAVEL" and item_key(i) not in completed_ids)

    request_lines = tuple(event_request_lines(events, original))
    summary: list[str] = []
    if completed_kept:
        summary.append(f"완료한 이전 일정 {completed_kept}개는 그대로 유지됩니다.")
    if after_n < before_n:
        summary.append(f"남은 활동: {before_n}곳 → {after_n}곳")
    removed_n = sum(1 for d in unique if d.kind == DiffKind.REMOVED)
    if removed_n:
        summary.append(f"{removed_n}개 장소가 남은 일정에서 제외됩니다.")
    time_n = sum(1 for d in unique if d.kind == DiffKind.TIME_CHANGED)
    if time_n:
        summary.append(f"이후 일정이 다시 조정됩니다. ({time_n}개 시간 변경)")
    if o_lodge and p_lodge and o_lodge != p_lodge:
        summary.append(f"숙소 도착: {o_lodge:%H:%M} → {p_lodge:%H:%M}")
    if replan and replan.main_status == "moved_meal":
        summary.append("선택한 기준 식당의 식사 시간대가 변경됩니다.")
    if replan and replan.main_status == "INFEASIBLE":
        summary.append("선택한 기준 장소를 현재 조건 안에 포함하기 어렵습니다.")

    return ReplanDiff(
        items=tuple(unique),
        completed_kept=completed_kept,
        request_lines=request_lines,
        summary_lines=tuple(summary),
    )


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    return f"{value:%H:%M}"


def confirmation_candidates(parse: ParseReplanResult, schedule: TripSchedule) -> list[tuple[str, str]]:
    """Place options for ambiguous skip/closed intents."""
    remaining_names: list[tuple[str, str]] = []
    for intent in parse.intents:
        if not intent.requires_confirmation:
            continue
        text = (intent.target_place_text or "").strip()
        for item in _flat_items(schedule):
            if item.item_type == "TRAVEL":
                continue
            if text and text in item.place_name:
                remaining_names.append((item.place_id, item.place_name))
            elif not text:
                remaining_names.append((item.place_id, item.place_name))
    # unique
    uniq: dict[str, str] = {}
    for pid, name in remaining_names:
        uniq[pid] = name
    if not uniq:
        # fallback: all future-ish visits
        for item in _flat_items(schedule):
            if item.item_type != "TRAVEL":
                uniq[item.place_id] = item.place_name
    return list(uniq.items())


def combine_current_datetime(day: date, clock: time, tzinfo) -> datetime:
    return datetime(
        day.year, day.month, day.day, clock.hour, clock.minute, clock.second,
        tzinfo=tzinfo)


def lodging_label(schedule: TripSchedule) -> str:
    if schedule.accommodation_status == "PROVISIONAL":
        name = schedule.accommodation.name if schedule.accommodation else "임시 기준점"
        return f"임시 기준점 · {name}"
    if schedule.accommodation:
        return f"숙소 · {schedule.accommodation.name}"
    return "숙소 정보 없음"


def return_preview_lines(schedule: TripSchedule) -> list[str]:
    lines: list[str] = []
    if schedule.trip_end_datetime:
        lines.append(f"귀가 목표 {schedule.trip_end_datetime:%H:%M}")
    if schedule.final_arrival_datetime:
        lines.append(f"변경 후 예상 귀가 {schedule.final_arrival_datetime:%H:%M}")
        if schedule.trip_end_datetime:
            margin = (schedule.trip_end_datetime - schedule.final_arrival_datetime).total_seconds() / 60
            lines.append(f"남은 여유 {int(margin)}분" if margin >= 0 else "귀가 목표 초과 위험")
    return lines


def finalize_parse_with_place(
        parse: ParseReplanResult,
        place_id: str,
        *,
        occurred_at: datetime | None = None,
) -> ParseReplanResult:
    """User picked an ambiguous place — fill place_id and emit ReplanEvents (no LLM)."""
    from services.replan_nl_parser import intents_to_events

    when = occurred_at or parse.occurred_at
    if when is None:
        when = datetime.now()
    updated: list = []
    for intent in parse.intents:
        if intent.requires_confirmation and intent.supported and intent.event_type != "UNSUPPORTED":
            updated.append(intent.model_copy(update={
                "target_place_id": place_id,
                "requires_confirmation": False,
                "ambiguity_reason": "",
            }))
        else:
            updated.append(intent)
    events, needs_confirm, notices = intents_to_events(updated, when)
    return parse.model_copy(update={
        "intents": tuple(updated),
        "events": events,
        "requires_confirmation": needs_confirm,
        "notices": notices,
    })


def unsupported_user_message(parse: ParseReplanResult) -> str | None:
    for intent in parse.intents:
        if not intent.supported or intent.event_type == "UNSUPPORTED":
            reason = intent.ambiguity_reason or intent.reason or ""
            if "날씨" in reason or "날씨" in (parse.user_text or ""):
                return (
                    "현재는 실시간 날씨를 자동 확인하지 않습니다. "
                    "비 때문에 제외하고 싶은 장소나 줄이고 싶은 활동을 알려주세요."
                )
            return reason or "이 요청은 현재 자동으로 반영하기 어렵습니다. 더 구체적으로 알려주세요."
    return None


def diff_kind_label(kind: DiffKind) -> str:
    return {
        DiffKind.REMOVED: "삭제",
        DiffKind.ADDED: "추가",
        DiffKind.TIME_CHANGED: "시간 변경",
        DiffKind.MEAL_CHANGED: "식사 시간대 변경",
        DiffKind.DAY_CHANGED: "일차 변경",
        DiffKind.RETURN_CHANGED: "귀가 변경",
        DiffKind.ACCOMMODATION_CHANGED: "숙소 변경",
        DiffKind.UNCHANGED_COMPLETED: "완료 유지",
    }.get(kind, kind.value)


@dataclass(frozen=True)
class PendingReplan:
    """Session-payload for Preview (never mutates active schedule until Apply)."""
    id: str
    user_text: str
    base_signature: str
    trip_signature: str
    replan: ReplanResult
    completed_keys: tuple[str, ...] = ()
    parse: ParseReplanResult | None = None
    diff: ReplanDiff | None = None


def can_apply_pending(
        pending: PendingReplan | dict,
        *,
        active_schedule: TripSchedule,
        trip: TripRequest,
        selected: TransportCandidate,
        preferred_place_id: str | None,
        lodging_key: str,
        already_applied_id: str | None = None,
) -> tuple[bool, str]:
    """Stale / double-apply / infeasible guards. Returns (ok, message)."""
    if isinstance(pending, dict):
        pid = pending.get("id")
        base_sig = pending.get("base_signature")
        trip_sig = pending.get("trip_signature")
        replan = pending.get("replan")
    else:
        pid = pending.id
        base_sig = pending.base_signature
        trip_sig = pending.trip_signature
        replan = pending.replan

    if already_applied_id and already_applied_id == pid:
        return False, "이미 적용된 변경안입니다."
    if base_sig != schedule_content_signature(active_schedule):
        return False, "기존 일정이 변경되어 변경안을 다시 계산해야 합니다."
    expected_trip = trip_replan_signature(
        trip, selected, preferred_place_id=preferred_place_id,
        lodging_key=lodging_key, schedule=active_schedule)
    if trip_sig != expected_trip:
        return False, "기존 일정이 변경되어 변경안을 다시 계산해야 합니다."
    if replan is None:
        return False, "적용할 변경안이 없습니다. 기존 일정은 그대로 유지됩니다."
    if replan.status == ReplanStatus.REPLAN_NO_CHANGE:
        return False, status_user_message(replan.status)
    if replan.status in (ReplanStatus.REPLAN_FAILED, ReplanStatus.REPLAN_INFEASIBLE):
        return False, status_user_message(replan.status)
    if replan.proposed_schedule is None:
        return False, "적용할 변경안이 없습니다. 기존 일정은 그대로 유지됩니다."
    return True, ""


def apply_pending_to_active(
        pending: PendingReplan | dict,
        active_schedule: TripSchedule,
) -> TripSchedule:
    """Return proposed schedule; caller replaces active. Does not mutate inputs."""
    replan = pending["replan"] if isinstance(pending, dict) else pending.replan
    if replan.proposed_schedule is None:
        return active_schedule
    # Identity check: proposed must be a different object / copy path from engine
    return replan.proposed_schedule
