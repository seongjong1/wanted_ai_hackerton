"""Multi-day accommodation schedules; reuses ScheduleService windows + return planning."""
import logging
from datetime import datetime, timedelta, time, date

from models.access import AccessPoint
from models.schedule import (ReturnStatus, ScheduleResult, TripDaySchedule, TripSchedule)
from models.transport import KST, TransportType
from providers.http_client import ProviderError
from services.access_service import AccessCache, AccessService
from services.location_service import normalize
from services.return_schedule_service import ReturnScheduleService, validate_return

logger = logging.getLogger("travel_ai.multiday")

# ~55m latitude band; used only when stable place/hub ids are unavailable.
_COORD_TOLERANCE = 0.0005


def arrival_hub_query(selected) -> str:
    """TAGO arrival short-name → hub resolve query (never TripRequest.destination alone)."""
    return AccessService.hub_query(
        selected.model_copy(update={"departure_place": selected.arrival_place}))


def same_access_point(left: AccessPoint | None, right: AccessPoint | None) -> bool:
    """Stable place match: id first, then normalized name + coordinate tolerance (not object identity)."""
    if left is None or right is None:
        return False
    if left.id == right.id:
        return True
    if normalize(left.name) != normalize(right.name):
        return False
    return abs(left.x - right.x) <= _COORD_TOLERANCE and abs(left.y - right.y) <= _COORD_TOLERANCE


def is_multiday(trip) -> bool:
    return bool(trip.has_accommodation and trip.end_date > trip.start_date)


def trip_dates(trip) -> list[date]:
    days = []
    cursor = trip.start_date
    while cursor <= trip.end_date:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def resolve_accommodation(access, query: str) -> AccessPoint:
    query = " ".join((query or "").strip().split())
    if not query:
        raise ProviderError("INVALID_ORIGIN")
    try:
        return access.resolve_origin(query)
    except ProviderError as first:
        try:
            return access.resolve_point(query)
        except ProviderError:
            pass
        # Lodging names are often ambiguous in OriginResolver; prefer a unique hotel/숙박 POI.
        try:
            documents = access.places.search_places(query, size=15)
        except ProviderError:
            documents = []
        lodging_keys = ("숙박", "호텔", "모텔", "리조트", "게스트하우스", "펜션")
        matches: dict[str, AccessPoint] = {}
        for row in documents:
            try:
                point = AccessPoint(
                    id=str(row["id"]), name=row["place_name"], x=float(row["x"]), y=float(row["y"]),
                    address=row.get("address_name", ""), category=row.get("category_name", ""),
                    original_query=query, source="kakao_local")
            except (KeyError, TypeError, ValueError):
                continue
            category = point.category
            name = point.name
            if query == name or query in name or any(key in category for key in lodging_keys):
                matches[point.id] = point
        if len(matches) == 1:
            return next(iter(matches.values()))
        # Exact lodging name wins when several POIs share a hotel complex.
        exact = {pid: p for pid, p in matches.items() if p.name == query or query in p.name}
        lodging_only = {pid: p for pid, p in exact.items()
                        if any(key in p.category for key in lodging_keys)}
        if len(lodging_only) == 1:
            return next(iter(lodging_only.values()))
        if first.code == "NEED_ADDRESS":
            try:
                return access.resolve_origin(query, query)
            except ProviderError:
                raise ProviderError("NEED_ADDRESS") from None
        try:
            return access.resolve_origin(query, query)
        except ProviderError:
            raise ProviderError("INVALID_ORIGIN") from None


def accommodation_error_message(code: str | None = None) -> str:
    if code == "NEED_ADDRESS":
        return "숙소 위치를 찾지 못했습니다. 숙소의 도로명 주소를 입력해주세요."
    return "숙소 위치를 찾지 못했습니다. 숙소의 도로명 주소를 입력해주세요."


def overnight_accommodations(dates: list, accommodation: AccessPoint) -> tuple[AccessPoint, ...]:
    """Phase 4.8: same lodging every night. Future: one AccessPoint per overnight date."""
    if len(dates) < 2:
        return ()
    return tuple(accommodation for _ in dates[:-1])


PROVISIONAL_NOTICE = (
    "숙소를 선택하지 않으면 도착 거점을 임시 기준점으로 일정을 계산합니다. "
    "추천 숙소 후보에서 숙소를 고르거나 직접 입력하면 전체 일정을 다시 계산합니다."
)


def validate_multiday(schedule, trip, accommodation) -> bool:
    reason = validate_multiday_reason(schedule, trip, accommodation)
    if reason:
        logger.warning("multiday_validate outcome=failed reason=%s status=%s",
                       reason, getattr(schedule, "accommodation_status", ""))
        return False
    return True


def validate_multiday_reason(schedule, trip, accommodation) -> str:
    """Empty string when valid; otherwise a stable internal failure reason (not shown raw in UI)."""
    dates = trip_dates(trip)
    if not schedule.days or len(schedule.days) != len(dates):
        return "day_count"
    if schedule.accommodation is None or not same_access_point(schedule.accommodation, accommodation):
        return "accommodation_mismatch"
    if [d.date for d in schedule.days] != dates:
        return "date_mismatch"
    if schedule.days[0].role != "FIRST" or schedule.days[-1].role != "FINAL":
        return "role_boundary"
    if any(d.role != "MIDDLE" for d in schedule.days[1:-1]):
        return "role_middle"
    # PROVISIONAL and CONFIRMED both use accommodation as the overnight day-boundary anchor.
    if not same_access_point(schedule.days[0].end_location, accommodation):
        return "first_end_accommodation"
    for day in schedule.days[1:]:
        if not same_access_point(day.start_location, accommodation):
            return "day_start_accommodation"
    for day in schedule.days[:-1]:
        if not same_access_point(day.end_location, accommodation):
            return "day_end_accommodation"
    visited = []
    for day in schedule.days:
        for item in day.items:
            if item.item_type != "TRAVEL":
                visited.append(item.place_id)
    if len(visited) != len(set(visited)):
        return "duplicate_place"
    if schedule.user_selected_place_id and schedule.anchor_status == "INCLUDED":
        if schedule.user_selected_place_id not in visited:
            return "anchor_missing"
    if schedule.return_status == ReturnStatus.RETURN_AVAILABLE:
        if schedule.return_journey is None or schedule.final_arrival_datetime is None:
            return "return_incomplete"
        deadline = datetime.combine(trip.end_date, trip.end_time, KST)
        if schedule.final_arrival_datetime > deadline:
            return "return_deadline"
    if schedule.validation_status != "VALIDATED":
        return "not_validated"
    return ""


class MultidayScheduleService:
    def __init__(self, activity, transport, access, returns: ReturnScheduleService):
        self.activity, self.transport, self.access, self.returns = activity, transport, access, returns

    def generate(self, trip, selected, candidates, preferred_place_id=None,
                 accommodation_query=None, *, accommodation_undecided: bool = False,
                 accommodation_point: AccessPoint | None = None):
        if selected is None:
            return ScheduleResult("교통편 선택 필요", notices=["교통편을 먼저 선택해주세요."])
        # Resolve the real arrival hub POI (coords + id). Never use TripRequest.destination alone.
        try:
            arrival = self.access.resolve_point(arrival_hub_query(selected))
        except ProviderError:
            return ScheduleResult("도착 위치 확인 필요", notices=["도착 거점의 위치를 확인하지 못해 일정을 생성하지 못했습니다."])

        notices = []
        if accommodation_undecided:
            # Provisional overnight base = resolved arrival hub AccessPoint (e.g. 구미종합터미널), not "구미".
            accommodation = arrival
            lodging_status = "PROVISIONAL"
            notices.append(PROVISIONAL_NOTICE)
        elif accommodation_point is not None:
            accommodation = accommodation_point
            lodging_status = "CONFIRMED"
        elif accommodation_query and str(accommodation_query).strip():
            try:
                accommodation = resolve_accommodation(self.access, str(accommodation_query).strip())
            except ProviderError as exc:
                return ScheduleResult("숙소 확인 필요", notices=[accommodation_error_message(exc.code)])
            lodging_status = "CONFIRMED"
        else:
            # UI owns the empty-input message near the lodging panel; avoid duplicate schedule warnings.
            return ScheduleResult("숙소 입력 필요", notices=[])

        dates = trip_dates(trip)
        if len(dates) > self.activity.config.max_schedule_days:
            return ScheduleResult("기간 확인 필요", notices=[
                f"현재 일정은 최대 {self.activity.config.max_schedule_days}일 범위에서 생성합니다."])

        config = self.activity.config
        daily_start = config.default_daily_start_hour
        daily_end = config.default_daily_end_hour
        home_deadline = datetime.combine(trip.end_date, trip.end_time, KST)
        days_out: list[TripDaySchedule] = []
        all_items = []
        exclude: set[str] = set()
        ai_status = "미사용"
        preferred_remaining = preferred_place_id
        preferred_day_index = None
        recorded_meal_role = ""

        # Phase 4.7: restaurant MAIN may fit a later day better than a late DAY-1 arrival.
        if preferred_place_id:
            from services.schedule_service import _preferred_plan_score
            best_probe = None
            for index, day in enumerate(dates[:-1]):
                role = "FIRST" if index == 0 else "MIDDLE"
                if role == "FIRST":
                    start = selected.arrival_time
                    start_point = arrival
                    deadline = datetime.combine(day, time(daily_end), KST)
                else:
                    start = datetime.combine(day, time(daily_start), KST)
                    start_point = accommodation
                    deadline = datetime.combine(day, time(daily_end), KST)
                probe = self.activity.generate_window(
                    trip, selected, list(candidates), start=start, deadline=deadline,
                    start_point=start_point, preferred_place_id=preferred_place_id,
                    exclude_ids=frozenset(), end_point=accommodation)
                if not probe.schedule:
                    continue
                if not any(i.place_id == preferred_place_id and i.item_type != "TRAVEL"
                           for i in probe.schedule.items):
                    continue
                score = _preferred_plan_score(probe.schedule.items, preferred_place_id)
                if best_probe is None or score > best_probe[0]:
                    best_probe = (score, index, probe.schedule.anchor_meal_role or "")
            if best_probe is not None:
                preferred_day_index = best_probe[1]
                recorded_meal_role = best_probe[2]
                logger.info("multiday_main outcome=day_selected index=%d meal_role=%s",
                            preferred_day_index, recorded_meal_role)

        for index, day in enumerate(dates):
            role = "FIRST" if index == 0 else ("FINAL" if index == len(dates) - 1 else "MIDDLE")
            if preferred_day_index is not None:
                preferred = preferred_place_id if index == preferred_day_index else None
            else:
                preferred = preferred_remaining
            if role == "FIRST":
                start = selected.arrival_time
                start_point = arrival
                deadline = datetime.combine(day, time(daily_end), KST)
                end_point = accommodation
            elif role == "MIDDLE":
                start = datetime.combine(day, time(daily_start), KST)
                start_point = accommodation
                deadline = datetime.combine(day, time(daily_end), KST)
                end_point = accommodation
            else:
                start = datetime.combine(day, time(daily_start), KST)
                start_point = accommodation
                # Final-day activities end at return cutoff; return attached afterwards.
                deadline = home_deadline
                end_point = None

            if role == "FINAL":
                # Activities are rebuilt against return cutoff in _attach_return.
                days_out.append(TripDaySchedule(
                    day_index=index + 1, date=day, role=role, start_location=accommodation,
                    end_location=accommodation, activity_start=start, activity_end=start, items=()))
                continue

            pool = list(candidates)
            result = self.activity.generate_window(
                trip, selected, pool, start=start, deadline=deadline, start_point=start_point,
                preferred_place_id=preferred, exclude_ids=frozenset(exclude), end_point=end_point)
            if not result.schedule or not result.schedule.items:
                return ScheduleResult(result.status or "일정 생성 실패",
                                      notices=notices + result.notices)
            day_items = result.schedule.items
            # Avoid repeating "MAIN infeasible" notices when MAIN is reserved for another day.
            for notice in result.notices:
                if preferred is None and "포함하기 어렵습니다" in notice:
                    continue
                notices.append(notice)
            ai_status = result.ai_status or ai_status
            if result.schedule.anchor_meal_role:
                recorded_meal_role = result.schedule.anchor_meal_role
            end_location = end_point
            activity_end = day_items[-1].end_datetime
            for item in day_items:
                if item.item_type != "TRAVEL":
                    exclude.add(item.place_id)
            if preferred_remaining and any(
                    i.place_id == preferred_remaining and i.item_type != "TRAVEL" for i in day_items):
                preferred_remaining = None
            days_out.append(TripDaySchedule(
                day_index=index + 1, date=day, role=role, start_location=start_point,
                end_location=end_location, activity_start=start, activity_end=activity_end,
                items=tuple(day_items)))
            all_items.extend(day_items)

        notices = list(dict.fromkeys(notices))

        # Attach return on final day only, using end_date schedules.
        final = days_out[-1]
        last_point = final.end_location
        last_time = final.activity_end
        return_result = self._attach_return(
            trip, selected, candidates, preferred_place_id, accommodation, arrival,
            last_point, last_time, all_items, days_out, home_deadline, lodging_status)
        if return_result.schedule is None:
            status = getattr(return_result, "return_status", ReturnStatus.RETURN_INFEASIBLE)
            included = bool(preferred_place_id and any(
                i.place_id == preferred_place_id and i.item_type != "TRAVEL" for i in all_items))
            schedule = TripSchedule(
                trip_start_datetime=selected.arrival_time, trip_end_datetime=home_deadline,
                arrival_point=arrival, items=tuple(all_items), days=tuple(days_out),
                accommodation=accommodation, accommodation_status=lodging_status,
                accommodation_nights=overnight_accommodations(dates, accommodation),
                return_status=status,
                user_selected_place_id=preferred_place_id,
                anchor_status=("INCLUDED" if included else "INFEASIBLE" if preferred_place_id else ""),
                anchor_meal_role=recorded_meal_role if included else (
                    "" if not preferred_place_id else recorded_meal_role),
                validation_status="VALIDATED",
                return_transport_candidates=getattr(return_result, "candidates", ()) or ())
            if preferred_place_id and not included:
                notices.append("선택한 장소를 현재 일정 조건 안에 포함하기 어렵습니다.")
            if any("확인하지 못했습니다" in n for n in return_result.notices):
                schedule = schedule.model_copy(update={"return_status": ReturnStatus.RETURN_UNKNOWN})
            elif any("어렵습니다" in n or "귀가하기 어렵" in n for n in return_result.notices):
                schedule = schedule.model_copy(update={"return_status": ReturnStatus.RETURN_INFEASIBLE})
            if not validate_multiday(schedule, trip, accommodation):
                return ScheduleResult("일정 검증 실패", notices=list(dict.fromkeys(
                    notices + return_result.notices + ["날짜별 숙소 연결을 검증하지 못했습니다."])))
            return ScheduleResult("생성 완료", schedule, list(dict.fromkeys(notices + return_result.notices)), ai_status)

        schedule = return_result.schedule
        if recorded_meal_role and not schedule.anchor_meal_role:
            schedule = schedule.model_copy(update={"anchor_meal_role": recorded_meal_role})
        if preferred_place_id and schedule.anchor_status == "INFEASIBLE":
            notices.append("선택한 장소를 현재 일정 조건 안에 포함하기 어렵습니다.")
        if not validate_multiday(schedule, trip, accommodation):
            return ScheduleResult("일정 검증 실패", notices=list(dict.fromkeys(notices + [
                "날짜별 숙소 연결을 검증하지 못했습니다."])))
        return ScheduleResult("생성 완료", schedule, list(dict.fromkeys(notices + return_result.notices)), ai_status)

    def _attach_return(self, trip, selected, candidates, preferred_place_id, accommodation,
                       arrival, last_point, last_time, all_items, days_out, home_deadline,
                       lodging_status: str = "CONFIRMED"):
        """Run return search on end_date only; never judge day-1 by same-day return."""
        cache = AccessCache()
        notices = []
        try:
            origin = selected.access.access_leg.origin_point if selected.access else None
            # Return hub selection needs a regional address (시·도/시·군·구); station-only POI names are not enough.
            if origin is None or len((origin.address or "").split()) < 2:
                origin = self.access.resolve_origin(trip.departure)
            cache.points[trip.departure] = origin
            cache.points[accommodation.id] = accommodation
            cache.points[last_point.id] = last_point
        except ProviderError:
            return ScheduleResult("귀가 조회 실패", notices=[
                "귀가 교통편을 확인하지 못했습니다. 가는 교통편은 계속 확인할 수 있습니다."])

        earliest = datetime.combine(trip.end_date, time(self.activity.config.default_daily_start_hour), KST)
        try:
            reverse = trip.model_copy(update={
                "departure": trip.destination,
                "destination": origin.address or origin.name,
                "start_date": trip.end_date,
                "departure_time": earliest.time().replace(tzinfo=None),
            })
            return_candidates = self.transport.search_return_candidates(
                reverse, origin, earliest_departure=earliest)
        except ProviderError:
            return ScheduleResult("귀가 조회 실패", notices=[
                "귀가 교통편을 확인하지 못했습니다. 가는 교통편은 계속 확인할 수 있습니다."])

        feasible = []
        for candidate in return_candidates:
            try:
                query = self.access.hub_query(candidate.model_copy(
                    update={"departure_place": candidate.arrival_place}))
                home_leg = self.access.get_leg(query, trip.departure, candidate.arrival_time, cache)
                if home_leg.arrival_time <= home_deadline:
                    feasible.append((candidate, home_leg))
            except ProviderError:
                continue
        if not feasible:
            status = ReturnStatus.RETURN_NONE if not return_candidates else ReturnStatus.RETURN_INFEASIBLE
            notice = ("가는 교통편은 조회되었지만 현재 귀가 완료 목표시간까지 가능한 왕복 일정을 만들기 어렵습니다."
                      if status == ReturnStatus.RETURN_INFEASIBLE else
                      "현재 조건에서 귀가 교통편 후보를 찾지 못했습니다. 가는 교통편은 계속 확인할 수 있습니다.")
            result = ScheduleResult("귀가 조건 미충족", notices=[notice])
            result.return_status = status  # type: ignore[attr-defined]
            result.candidates = tuple(return_candidates[:5])  # type: ignore[attr-defined]
            return result

        # Prefer largest final-day activity window among home-feasible returns.
        scored = []
        morning = datetime.combine(trip.end_date, time(self.activity.config.default_daily_start_hour), KST)
        for candidate, home_leg in feasible:
            buffer = (self.access.train_buffer_minutes if candidate.transport_type == TransportType.TRAIN
                      else self.access.bus_buffer_minutes)
            hub_query = self.access.hub_query(candidate)
            try:
                if hub_query not in cache.points:
                    cache.points[hub_query] = self.access.resolve_point(hub_query)
                # Conservative cutoff from lodging → return hub (final day always starts at lodging).
                estimate = self.access.get_leg(accommodation.id, hub_query, morning, cache)
            except ProviderError:
                continue
            cutoff = candidate.departure_time - timedelta(minutes=buffer + estimate.duration_minutes)
            if cutoff <= morning:
                continue
            scored.append((cutoff, candidate, home_leg, buffer, hub_query))
        scored.sort(key=lambda row: (-row[0].timestamp(), row[2].arrival_time))

        for cutoff, candidate, home_leg, buffer, hub_query in scored[:5]:
            day_exclude = {i.place_id for d in days_out[:-1] for i in d.items if i.item_type != "TRAVEL"}
            final_preferred = (preferred_place_id
                               if preferred_place_id and preferred_place_id not in day_exclude else None)
            hub_point = cache.points[hub_query]
            previous_anchor = self.activity.return_anchor
            # Final-day gap fill must see return hub so trailing free time to cutoff is usable.
            self.activity.return_anchor = hub_point
            try:
                rebuilt = self.activity.generate_window(
                    trip, selected, candidates, start=morning, deadline=cutoff,
                    start_point=accommodation, preferred_place_id=final_preferred,
                    exclude_ids=frozenset(day_exclude), end_point=None)
            finally:
                self.activity.return_anchor = previous_anchor
            if not rebuilt.schedule or not rebuilt.schedule.items:
                day_items = []
                last_place = accommodation
                last_end = morning
            else:
                day_items = list(rebuilt.schedule.items)
                last_place = None
                for item in reversed(day_items):
                    if item.item_type == "TRAVEL" and item.destination:
                        last_place = item.destination
                        break
                    if item.item_type != "TRAVEL":
                        last_place = AccessPoint(id=item.place_id, name=item.place_name,
                                                 x=accommodation.x, y=accommodation.y)
                        for prev in reversed(day_items):
                            if prev.item_type == "TRAVEL" and prev.destination and prev.destination.id == item.place_id:
                                last_place = prev.destination
                                break
                        break
                if last_place is None:
                    last_place = accommodation
                last_end = day_items[-1].end_datetime
            cache.points[last_place.id] = last_place
            try:
                leg = self.access.get_leg(last_place.id, hub_query, last_end, cache)
            except ProviderError:
                continue
            actual_cutoff = candidate.departure_time - timedelta(minutes=buffer + leg.duration_minutes)
            if last_end > actual_cutoff:
                continue
            from models.schedule import ReturnJourney
            journey = ReturnJourney(to_hub=leg, transport=candidate,
                                    boarding_buffer_minutes=buffer, to_origin=home_leg)
            final_day = days_out[-1].model_copy(update={
                "items": tuple(day_items),
                "end_location": last_place,
                "activity_end": max(last_end, days_out[-1].activity_start),
            })
            new_days = tuple(days_out[:-1]) + (final_day,)
            flat = [i for d in new_days for i in d.items]
            included = bool(preferred_place_id and any(
                i.place_id == preferred_place_id and i.item_type != "TRAVEL" for i in flat))
            meal_role = ""
            if rebuilt.schedule and rebuilt.schedule.anchor_meal_role and included:
                meal_role = rebuilt.schedule.anchor_meal_role
            elif included and preferred_place_id:
                pref_item = next(i for i in flat if i.place_id == preferred_place_id and i.item_type != "TRAVEL")
                if pref_item.meal_slot and pref_item.meal_slot.endswith("점심"):
                    meal_role = "LUNCH"
                elif pref_item.meal_slot and pref_item.meal_slot.endswith("저녁"):
                    meal_role = "DINNER"
                else:
                    meal_role = "NONE"
            schedule = TripSchedule(
                trip_start_datetime=selected.arrival_time, trip_end_datetime=home_deadline,
                arrival_point=arrival, items=tuple(flat), days=new_days,
                accommodation=accommodation, accommodation_status=lodging_status,
                accommodation_nights=overnight_accommodations(
                    [d.date for d in new_days], accommodation),
                return_journey=journey,
                return_transport_candidates=tuple(c for c, _ in feasible[:5]),
                return_status=ReturnStatus.RETURN_AVAILABLE,
                final_arrival_datetime=home_leg.arrival_time,
                destination_activity_cutoff=actual_cutoff,
                user_selected_place_id=preferred_place_id,
                anchor_status=("INCLUDED" if included else "INFEASIBLE" if preferred_place_id else ""),
                anchor_meal_role=meal_role,
                validation_status="VALIDATED")
            if day_items and day_items[-1].item_type == "TRAVEL":
                continue
            ok = False
            if not day_items:
                ok = (leg.origin_point is not None and same_access_point(leg.origin_point, accommodation)
                      and home_leg.arrival_time <= home_deadline
                      and home_leg.destination_point == origin)
            else:
                ok = validate_return(schedule, trip, origin, cache)
            if ok:
                logger.info("multiday_return outcome=valid date=%s cutoff=%s",
                            trip.end_date.isoformat(), actual_cutoff.isoformat())
                return ScheduleResult("생성 완료", schedule, notices)
        result = ScheduleResult("귀가 조건 미충족", notices=[
            "가는 교통편은 조회되었지만 현재 귀가 완료 목표시간까지 가능한 왕복 일정을 만들기 어렵습니다."])
        result.return_status = ReturnStatus.RETURN_INFEASIBLE  # type: ignore[attr-defined]
        result.candidates = tuple(c for c, _ in feasible[:5])  # type: ignore[attr-defined]
        return result
