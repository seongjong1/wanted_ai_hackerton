"""Backward return planning kept separate from outbound transport search."""
import logging
from datetime import datetime, timedelta, time

from models.schedule import ReturnJourney, ReturnStatus, ScheduleResult
from models.transport import KST, TransportType
from providers.http_client import ProviderError
from services.access_service import AccessCache
from services.schedule_service import point_for

logger = logging.getLogger("travel_ai.return")


def validate_return(schedule, trip, origin, cache):
    journey = schedule.return_journey
    deadline = datetime.combine(trip.end_date, trip.end_time, KST)
    if journey is None or not schedule.items or schedule.trip_end_datetime != deadline:
        return False
    if schedule.return_status != ReturnStatus.RETURN_AVAILABLE:
        return False
    last = schedule.items[-1]
    a, transport, b = journey.to_hub, journey.transport, journey.to_origin
    from services.access_service import AccessService
    departure_hub = cache.points.get(AccessService.hub_query(transport))
    arrival_hub = cache.points.get(AccessService.hub_query(
        transport.model_copy(update={"departure_place": transport.arrival_place})))
    if a.destination_point != departure_hub or b.origin_point != arrival_hub or departure_hub is None or arrival_hub is None:
        return False
    for leg in (a, b):
        if not leg.origin_point or not leg.destination_point:
            return False
        if leg.origin_point.id == leg.destination_point.id:
            if leg.duration_minutes != 0:
                return False
        else:
            route = cache.routes.get((leg.origin_point.id, leg.destination_point.id))
            if route is None or abs(route.duration_seconds - leg.duration_minutes * 60) > .0001 or leg.steps != route.steps:
                return False
    if schedule.destination_activity_cutoff is not None and last.end_datetime > schedule.destination_activity_cutoff:
        return False
    return (last.item_type != "TRAVEL" and a.origin_point is not None
            and a.origin_point.id == last.place_id and a.departure_time >= last.end_datetime
            and a.arrival_time + timedelta(minutes=journey.boarding_buffer_minutes) <= transport.departure_time
            and transport.arrival_time > transport.departure_time
            and b.departure_time == transport.arrival_time and b.destination_point == origin
            and schedule.final_arrival_datetime == b.arrival_time <= deadline
            and (not schedule.user_selected_place_id or any(
                i.place_id == schedule.user_selected_place_id and i.item_type != "TRAVEL" for i in schedule.items)))


class ReturnScheduleService:
    def __init__(self, activity, transport, access):
        self.activity, self.transport, self.access = activity, transport, access

    def generate(self, trip, selected, candidates, preferred_place_id=None, accommodation_query=None,
                 *, accommodation_undecided: bool = False, accommodation_point=None):
        from services.multiday_schedule_service import MultidayScheduleService, is_multiday
        if is_multiday(trip):
            return MultidayScheduleService(self.activity, self.transport, self.access, self).generate(
                trip, selected, candidates, preferred_place_id, accommodation_query,
                accommodation_undecided=accommodation_undecided,
                accommodation_point=accommodation_point)
        if selected is None:
            return ScheduleResult("교통편 선택 필요", notices=["교통편을 먼저 선택해주세요."])
        # Single-day / no-accommodation path ignores accommodation_query.
        return self._generate_single(trip, selected, candidates, preferred_place_id)

    def _generate_single(self, trip, selected, candidates, preferred_place_id=None):
        if selected is None:
            return ScheduleResult("교통편 선택 필요", notices=["교통편을 먼저 선택해주세요."])
        anchor = next((c for c in candidates if c.place_id == preferred_place_id), None)
        if preferred_place_id and anchor is None:
            return ScheduleResult("기준 장소 확인 필요", notices=["선택 장소를 다시 검색해주세요."])
        deadline = datetime.combine(trip.end_date, trip.end_time, KST)
        cache = AccessCache()
        pool = [anchor] if anchor else [c for c in candidates if c.role == "MAIN_DESTINATION"]
        if not pool:
            pool = list(candidates)

        try:
            origin = selected.access.access_leg.origin_point if selected.access else None
            if origin is None or len((origin.address or "").split()) < 2:
                origin = self.access.resolve_origin(trip.departure)
            cache.points[trip.departure] = origin
        except ProviderError:
            return ScheduleResult("귀가 조회 실패", notices=[
                "출발지 위치를 확인하지 못했습니다. 가는 교통편은 계속 확인할 수 있습니다."])

        return_candidates = []
        try:
            reverse = trip.model_copy(update={
                "departure": trip.destination,
                "destination": origin.address or origin.name,
                "start_date": trip.end_date,
                "departure_time": (selected.arrival_time.time().replace(tzinfo=None)
                                   if trip.end_date == selected.arrival_time.date() else time(0)),
            })
            return_candidates = self.transport.search_return_candidates(
                reverse, origin, earliest_departure=selected.arrival_time)
        except ProviderError:
            logger.warning("return_search outcome=unknown")
            return self._activity_only(trip, selected, pool, preferred_place_id, deadline,
                                       ReturnStatus.RETURN_UNKNOWN, (),
                                       ["귀가 교통편을 확인하지 못했습니다. 가는 교통편은 계속 확인할 수 있습니다."])

        feasible = []
        for candidate in return_candidates:
            try:
                query = self.access.hub_query(candidate.model_copy(
                    update={"departure_place": candidate.arrival_place}))
                home_leg = self.access.get_leg(query, trip.departure, candidate.arrival_time, cache)
                if home_leg.arrival_time <= deadline:
                    feasible.append((candidate, home_leg))
            except ProviderError:
                logger.info("return_access outcome=unknown")

        if not return_candidates:
            return_status = ReturnStatus.RETURN_NONE
        elif not feasible:
            return_status = ReturnStatus.RETURN_INFEASIBLE

        if not feasible:
            name = anchor.place_name if anchor else "현재 여행 조건"
            notice = (f"가는 교통편은 조회되었지만 {name}을 포함해 "
                      f"{trip.end_time:%H:%M}까지 가능한 왕복 일정을 만들기 어렵습니다.")
            return self._activity_only(trip, selected, pool, preferred_place_id, deadline,
                                       return_status, tuple(return_candidates[:5]), [notice])

        scored = []
        for candidate, home_leg in feasible:
            buffer = (self.access.train_buffer_minutes if candidate.transport_type == TransportType.TRAIN
                      else self.access.bus_buffer_minutes)
            hub_query = self.access.hub_query(candidate)
            try:
                if hub_query not in cache.points:
                    cache.points[hub_query] = self.access.resolve_point(hub_query)
            except ProviderError:
                continue
            estimate_id = anchor.place_id if anchor else None
            estimate_point = point_for(anchor) if anchor else None
            if estimate_point is not None:
                cache.points[estimate_id] = estimate_point
            seed = estimate_id
            if seed is None:
                # Fall back to destination arrival hub for a conservative cutoff estimate.
                arrival_hub = selected.arrival_place
                if selected.transport_type == TransportType.TRAIN:
                    arrival_hub = arrival_hub if arrival_hub.endswith("역") else arrival_hub + "역"
                elif "터미널" not in arrival_hub:
                    arrival_hub += "버스터미널"
                seed = arrival_hub
                try:
                    if seed not in cache.points:
                        cache.points[seed] = self.access.resolve_point(seed)
                except ProviderError:
                    continue
            try:
                estimate = self.access.get_leg(seed, hub_query, selected.arrival_time, cache)
            except ProviderError:
                continue
            cutoff = candidate.departure_time - timedelta(minutes=buffer + estimate.duration_minutes)
            if cutoff <= selected.arrival_time:
                continue
            scored.append((cutoff, candidate, home_leg, buffer, hub_query, estimate))
        # Prefer the largest destination activity window among home-feasible returns.
        scored.sort(key=lambda row: (-row[0].timestamp(), row[2].arrival_time,
                                     row[1].price if row[1].price is not None else float("inf")))

        for cutoff, candidate, home_leg, buffer, hub_query, _estimate in scored[:5]:
            attached = self._try_return(
                trip, selected, pool, preferred_place_id, deadline,
                cutoff, candidate, home_leg, buffer, hub_query, cache, origin,
                tuple(c for c, _ in feasible[:5]))
            if attached is not None:
                return attached

        # Spec: before declaring anchor infeasible, try a shorter stay window once.
        if preferred_place_id and scored:
            from dataclasses import replace
            original = self.activity.config
            self.activity.config = replace(
                original,
                meal_minutes=max(30, original.meal_minutes // 2),
                default_minutes=max(30, original.default_minutes // 2),
                sightseeing_minutes=max(30, original.sightseeing_minutes // 2),
                rest_minutes=max(20, original.rest_minutes // 2),
            )
            try:
                for cutoff, candidate, home_leg, buffer, hub_query, _estimate in scored[:5]:
                    attached = self._try_return(
                        trip, selected, pool, preferred_place_id, deadline,
                        cutoff, candidate, home_leg, buffer, hub_query, cache, origin,
                        tuple(c for c, _ in feasible[:5]))
                    if attached is not None:
                        attached.notices = [
                            "기준 장소 체류시간을 줄여 귀가 가능 일정을 만들었습니다."
                        ] + attached.notices
                        return attached
            finally:
                self.activity.config = original

        name = anchor.place_name if anchor else "현재 여행 조건"
        notice = (f"선택한 장소를 포함하면 {trip.end_time:%H:%M}까지 귀가하기 어렵습니다."
                  if anchor else
                  f"{name}을 포함해 {trip.end_time:%H:%M}까지 귀가하는 일정을 확인하지 못했습니다.")
        return self._activity_only(trip, selected, pool, preferred_place_id, deadline,
                                   ReturnStatus.RETURN_INFEASIBLE, tuple(c for c, _ in feasible[:5]),
                                   [notice])

    def _try_return(self, trip, selected, pool, preferred_place_id, deadline,
                    cutoff, candidate, home_leg, buffer, hub_query, cache, origin,
                    return_candidates):
        self.activity.return_anchor = cache.points[hub_query]
        working_cutoff = cutoff
        for _attempt in range(3):
            if working_cutoff <= selected.arrival_time:
                break
            activity_trip = trip.model_copy(update={
                "end_date": working_cutoff.date(),
                "end_time": working_cutoff.time().replace(tzinfo=None),
            })
            # Keep return hub as gap-fill anchor while rebuilding toward cutoff.
            previous_anchor = self.activity.return_anchor
            self.activity.return_anchor = cache.points[hub_query]
            try:
                result = self.activity.generate(activity_trip, selected, pool, preferred_place_id)
            finally:
                self.activity.return_anchor = previous_anchor
            if not result.schedule:
                break
            schedule = result.schedule
            if preferred_place_id and not any(i.place_id == preferred_place_id and i.item_type != "TRAVEL"
                                              for i in schedule.items):
                break
            last = schedule.items[-1]
            source = next((i.destination for i in reversed(schedule.items) if i.item_type == "TRAVEL"),
                          schedule.arrival_point)
            cache.points[last.place_id] = source
            try:
                leg = self.access.get_leg(last.place_id, hub_query, last.end_datetime, cache)
            except ProviderError:
                break
            actual_cutoff = candidate.departure_time - timedelta(minutes=buffer + leg.duration_minutes)
            if last.end_datetime > actual_cutoff:
                working_cutoff = min(working_cutoff, actual_cutoff)
                continue
            journey = ReturnJourney(to_hub=leg, transport=candidate,
                                    boarding_buffer_minutes=buffer, to_origin=home_leg)
            schedule = schedule.model_copy(update={
                "return_journey": journey,
                "return_transport_candidates": return_candidates,
                "return_status": ReturnStatus.RETURN_AVAILABLE,
                "trip_end_datetime": deadline,
                "final_arrival_datetime": home_leg.arrival_time,
                "destination_activity_cutoff": actual_cutoff,
                "user_selected_place_id": preferred_place_id,
                "anchor_status": "INCLUDED" if preferred_place_id else "",
            })
            if validate_return(schedule, trip, origin, cache):
                logger.info("return_schedule outcome=valid cutoff=%s", actual_cutoff.isoformat())
                return ScheduleResult("생성 완료", schedule, result.notices, result.ai_status)
            break
        return None

    def _activity_only(self, trip, selected, pool, preferred_place_id, deadline,
                       return_status, return_candidates, notices):
        # Destination activities may still use end_time as an upper bound when return is unknown.
        result = self.activity.generate(trip, selected, pool, preferred_place_id)
        if not result.schedule:
            return ScheduleResult(
                "귀가 조건 미충족" if return_status != ReturnStatus.RETURN_UNKNOWN else "귀가 조회 실패",
                notices=notices + result.notices)
        schedule = result.schedule.model_copy(update={
            "trip_end_datetime": deadline,
            "return_status": return_status,
            "return_transport_candidates": return_candidates,
            "user_selected_place_id": preferred_place_id,
            "anchor_status": ("INCLUDED" if preferred_place_id and any(
                i.place_id == preferred_place_id and i.item_type != "TRAVEL"
                for i in result.schedule.items) else
                "INFEASIBLE" if preferred_place_id else ""),
        })
        return ScheduleResult(result.status, schedule, notices + result.notices, result.ai_status)
