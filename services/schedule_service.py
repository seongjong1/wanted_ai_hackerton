"""Bounded route-backed scheduling. The LLM can prefer IDs, never author times."""
import logging
from datetime import datetime, timedelta
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import Settings, ScheduleSettings
from models.access import AccessPoint, AccessRoute
from models.place import PlaceCandidate
from models.schedule import ScheduleItem, TripSchedule, ScheduleResult
from models.transport import KST, TransportCandidate, TransportType
from models.trip_request import TripRequest, Preference
from providers.http_client import HttpClient, ProviderError
from providers.kakao_provider import KakaoProvider
from providers.kakao_transit_provider import KakaoTransitProvider
from providers.groq_provider import GroqProvider
from services.access_service import AccessService
from services.place_service import distance_meters, activity_radius_meters
from services.itinerary_suitability import (
    VisitRole, assess_place, classify_visit_role, refine_roles_with_groq, schedule_eligible,
    suitability_score, is_micro_landmark, activity_bucket, visited_buckets, diversity_penalty,
    allow_cafe_candidate, cafe_imbalanced)
from services.meal_role import MealRole, resolve_meal_role, is_restaurant

logger = logging.getLogger("travel_ai.schedule")


class ScheduleChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_ids: list[str] = Field(max_length=15)


def point_for(candidate: PlaceCandidate) -> AccessPoint:
    return AccessPoint(id=candidate.place_id, name=candidate.place_name,
                       x=candidate.longitude, y=candidate.latitude)


def is_meal(candidate: PlaceCandidate) -> bool:
    return ("음식점" in candidate.category or Preference.FOOD in candidate.matched_preferences) and not any(
        term in candidate.category for term in ("카페", "커피전문점"))


def activity_category(candidate: PlaceCandidate) -> str:
    return activity_bucket(candidate)


def suitability_score_for(candidate: PlaceCandidate, trip: TripRequest,
                          role: VisitRole | None = None) -> int:
    return suitability_score(candidate, trip, role)


def duration_for(candidate: PlaceCandidate, preference: Preference, config: ScheduleSettings,
                 trip: TripRequest | None = None, role: VisitRole | None = None) -> int:
    meal = is_meal(candidate)
    if trip is None:
        # Legacy path for callers without trip context — still avoid 60m for micro-landmarks.
        from services.itinerary_suitability import is_micro_landmark
        if not meal and is_micro_landmark(candidate):
            return config.short_stop_minutes
        if meal:
            return config.meal_minutes
        return {Preference.SIGHTSEEING: config.sightseeing_minutes, Preference.SHOPPING: config.shopping_minutes,
                Preference.EXPERIENCE: config.experience_minutes, Preference.REST: config.rest_minutes}.get(
                    preference, config.default_minutes)
    assessment = assess_place(candidate, trip, preference, config, meal=meal, role_override=role)
    return assessment.duration_minutes


def hour_on(day: datetime, hour: int) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=hour)


def visit_slot(now: datetime, deadline: datetime, duration: int, meal: bool,
               preference: Preference, used_meals: set[str], config: ScheduleSettings,
               *, allowed_meal_labels: tuple[str, ...] | None = None):
    """Pick the earliest feasible visit window.

    For meals, ``allowed_meal_labels`` may restrict to 점심/저녁 (FLEXIBLE probes pass one label).
    UNKNOWN opening hours never close a window — only time/route constraints do.
    """
    day = now
    while day.date() <= deadline.date():
        windows = config.meal_windows if meal else (("", max(config.day_start_hour,
            config.night_start_hour if preference == Preference.NIGHT_VIEW else config.day_start_hour), config.day_end_hour),)
        for label, start_hour, end_hour in windows:
            if meal and allowed_meal_labels is not None and label not in allowed_meal_labels:
                continue
            if meal and label == "점심" and preference == Preference.FOOD:
                end_hour = max(end_hour, config.late_lunch_end_hour)
            key = f"{day.date()}:{label}" if meal else None
            start = max(now, hour_on(day, start_hour))
            end = start + timedelta(minutes=duration)
            if key not in used_meals and end <= min(deadline, hour_on(day, end_hour)):
                return start, end, key
        day = hour_on(day, 24)
    return None


def _preferred_plan_score(items, preferred_id: str) -> tuple:
    """Higher is better: preferred included, more visits, less travel, shorter long gaps."""
    if not any(i.place_id == preferred_id and i.item_type != "TRAVEL" for i in items):
        return (-1,)
    visits = sum(1 for i in items if i.item_type != "TRAVEL")
    travel = sum(i.duration_minutes for i in items if i.item_type == "TRAVEL")
    long_gaps = 0.0
    prev_end = None
    for item in items:
        if prev_end is not None:
            gap = (item.start_datetime - prev_end).total_seconds() / 60
            if gap >= 90:
                long_gaps += gap
        prev_end = item.end_datetime
    return (1, visits, -travel, -long_gaps)


def validate_schedule(schedule: TripSchedule, trip: TripRequest, selected: TransportCandidate,
                      candidates: dict[str, PlaceCandidate], routes: dict[tuple[str, str], AccessRoute | None],
                      arrival: AccessPoint, config: ScheduleSettings, supporting_ids=frozenset()) -> bool:
    """Recheck source IDs, time intervals, movement continuity and independent route evidence."""
    deadline = datetime.combine(trip.end_date, trip.end_time, KST)
    activity_deadline = schedule.destination_activity_cutoff or deadline
    if schedule.trip_start_datetime != selected.arrival_time or schedule.trip_end_datetime != deadline:
        return False
    if schedule.arrival_point != arrival or not schedule.items:
        return False
    current, previous_end = arrival, selected.arrival_time
    visited, meals = set(), set()
    pending_travel = False
    for item in schedule.items:
        candidate = candidates.get(item.place_id)
        if candidate is None or item.place_name != candidate.place_name or item.open_status != "UNKNOWN":
            return False
        if item.start_datetime < previous_end or item.end_datetime <= item.start_datetime or item.end_datetime > activity_deadline:
            return False
        target = point_for(candidate)
        if item.item_type == "TRAVEL":
            route = routes.get((current.id, target.id))
            if pending_travel or item.origin != current or item.destination != target or route is None:
                return False
            if abs(item.duration_minutes * 60 - route.duration_seconds) > 0.0001:
                return False
            if item.travel_duration_minutes != route.duration_seconds / 60 or item.travel_mode != tuple(dict.fromkeys(s.mode for s in route.steps)):
                return False
            current, pending_travel = target, True
        else:
            if current.id != target.id or item.place_id in visited:
                return False
            try:
                preference = Preference(item.preference)
            except ValueError:
                return False
            if (preference not in trip.preferences and candidate.place_id not in supporting_ids) or preference not in candidate.matched_preferences:
                return False
            meal = is_meal(candidate)
            if (item.item_type == "MEAL") != meal or not item.estimated_duration:
                return False
            required = ["영업시간 확인 필요"]
            if trip.allergies and meal:
                required.append("알레르기 정보 확인 필요")
            if trip.has_pet:
                required.append("반려동물 동반 가능 여부 확인 필요")
            if trip.has_child:
                required.append("아이 동반 이용 조건 확인 필요")
            if not set(required).issubset(item.checks):
                return False
            duration = duration_for(candidate, preference, config, trip)
            if item.duration_minutes != duration:
                return False
            slot = visit_slot(item.start_datetime, activity_deadline, duration, meal, preference, meals, config)
            if slot != (item.start_datetime, item.end_datetime, item.meal_slot):
                return False
            if meal:
                meals.add(item.meal_slot)
            visited.add(item.place_id)
            pending_travel = False
        previous_end = item.end_datetime
    if schedule.user_selected_place_id and schedule.anchor_status == "INCLUDED":
        if schedule.user_selected_place_id not in visited:
            return False
    return bool(visited) and not pending_travel


class ScheduleService:
    def __init__(self, transit: KakaoTransitProvider, resolve_point: Callable[[str], AccessPoint],
                 *, config: ScheduleSettings | None = None, groq: GroqProvider | None = None,
                 extended_radius: int = 2000, supporting_search=None):
        self.transit, self.resolve_point, self.groq = transit, resolve_point, groq
        self.config = config or ScheduleSettings()
        self.extended_radius = extended_radius
        self.supporting_search = supporting_search
        self.return_anchor = None
        self._role_overrides: dict[str, VisitRole] = {}

    def generate(self, trip: TripRequest, selected: TransportCandidate | None,
                 candidates: list[PlaceCandidate], preferred_place_id: str | None = None,
                 *, preferred_meal_role: MealRole | str | None = None) -> ScheduleResult:
        if selected is None:
            return ScheduleResult("교통편 선택 필요", notices=["교통편을 먼저 선택해주세요."])
        hub = selected.arrival_place
        if selected.transport_type == TransportType.TRAIN:
            hub = hub if hub.endswith("역") else hub + "역"
        elif "터미널" not in hub:
            hub += "버스터미널"
        try:
            arrival = self.resolve_point(hub)
        except ProviderError:
            return ScheduleResult("도착 위치 확인 필요", notices=["도착 거점의 위치를 확인하지 못해 일정을 생성하지 못했습니다."])
        deadline = datetime.combine(trip.end_date, trip.end_time, KST)
        return self.generate_window(trip, selected, candidates, start=selected.arrival_time,
                                    deadline=deadline, start_point=arrival,
                                    preferred_place_id=preferred_place_id,
                                    preferred_meal_role=preferred_meal_role)

    def generate_window(self, trip: TripRequest, selected: TransportCandidate,
                        candidates: list[PlaceCandidate], *, start: datetime, deadline: datetime,
                        start_point: AccessPoint, preferred_place_id: str | None = None,
                        exclude_ids: frozenset[str] = frozenset(),
                        end_point: AccessPoint | None = None,
                        preferred_meal_role: MealRole | str | None = None) -> ScheduleResult:
        """Build activities inside [start, deadline] from start_point; optionally finish at end_point."""
        if start >= deadline:
            return ScheduleResult("시간 부족", notices=["선택한 교통편으로 도착하면 여행 가능 시간이 부족합니다."])
        if (deadline.date() - start.date()).days >= self.config.max_schedule_days:
            return ScheduleResult("기간 확인 필요", notices=[f"현재 일정은 도착일 포함 최대 {self.config.max_schedule_days}일 범위에서 생성합니다."])
        window_trip = trip.model_copy(update={"end_date": deadline.date(),
                                              "end_time": deadline.time().replace(tzinfo=None)})
        window_selected = selected.model_copy(update={"arrival_time": start})
        self._user_preferences = tuple(trip.preferences)
        available = {}
        for c in candidates:
            if c.place_id in exclude_ids:
                continue
            if not set(c.matched_preferences) & set(trip.preferences):
                continue
            old = available.get(c.place_id)
            if old is None or (old.role == "NEARBY_PLACE" and c.role == "MAIN_DESTINATION"):
                available[c.place_id] = c
        if not available:
            return ScheduleResult("후보 없음", notices=["먼저 방문 장소 후보를 검색해주세요."])
        # Deterministic itinerary roles; optional Groq assist only when micro-landmarks appear.
        rule_roles = {pid: classify_visit_role(c, trip) for pid, c in available.items()}
        if self.groq and any(is_micro_landmark(c) for c in available.values()):
            self._role_overrides = refine_roles_with_groq(
                self.groq, list(available.values()), trip, rule_roles)
        else:
            self._role_overrides = rule_roles
        ranked = sorted(available.values(), key=lambda c: (
            c.place_id != preferred_place_id,
            -suitability_score_for(c, trip, self._role_overrides.get(c.place_id)),
            -c.preference_score, c.place_id))
        seeds = [available[preferred_place_id]] if preferred_place_id in available else []
        for preference in trip.preferences:
            match = next((c for c in ranked if preference in c.matched_preferences
                          and schedule_eligible(assess_place(
                              c, trip, preference, self.config, meal=is_meal(c),
                              role_override=self._role_overrides.get(c.place_id)),
                              self.config, preferred=c.place_id == preferred_place_id)), None)
            if match is not None and match not in seeds:
                seeds.append(match)
        for preference in trip.preferences:
            nearby = next((c for c in ranked if c.role == "NEARBY_PLACE" and preference in c.matched_preferences
                           and schedule_eligible(assess_place(
                               c, trip, preference, self.config, meal=is_meal(c),
                               role_override=self._role_overrides.get(c.place_id)), self.config)), None)
            if nearby is not None and nearby not in seeds:
                seeds.append(nearby)
        ordered = (seeds + [c for c in ranked if c not in seeds])[:self.config.candidate_limit]
        available = {c.place_id: c for c in ordered}
        arrival = start_point
        activity_deadline = deadline
        routes: dict[tuple[str, str], AccessRoute | None] = {}
        if end_point is not None:
            if end_point.id != arrival.id:
                # Reserve time to reach accommodation / return-prep endpoint.
                try:
                    reserve = self.transit.fastest_route(arrival, end_point)
                    routes[(arrival.id, end_point.id)] = reserve
                    activity_deadline = min(deadline, deadline - timedelta(seconds=reserve.duration_seconds))
                except ProviderError:
                    routes[(arrival.id, end_point.id)] = None
            else:
                # Middle day: start and end at the same lodging — keep a config buffer for the return leg.
                activity_deadline = min(
                    deadline,
                    deadline - timedelta(minutes=self.config.accommodation_return_buffer_minutes))
        if start >= activity_deadline:
            return ScheduleResult("시간 부족", notices=["숙소·귀가 이동 시간을 확보하면 활동 시간이 부족합니다."])
        ai_ids, ai_status = [], "미사용"
        if self.groq:
            try:
                choice = self.groq.generate_structured(
                    "Select a useful preference-balanced order from supplied IDs. Output IDs only. "
                    "Do not invent times, opening hours, addresses or safety claims. Python verifies routes and schedules.",
                    {"preferences": [p.value for p in trip.preferences], "arrival": start.isoformat(),
                     "end": activity_deadline.isoformat(), "candidates": [{"place_id": c.place_id, "category": c.category,
                         "preferences": [p.value for p in c.matched_preferences]} for c in ordered]}, ScheduleChoice)
                if len(set(choice.place_ids)) != len(choice.place_ids) or any(i not in available for i in choice.place_ids):
                    raise ValueError("Unknown or duplicate AI place ID")
                ai_ids, ai_status = choice.place_ids, "AI 보조"
            except (ProviderError, ValidationError, ValueError):
                ai_status = "기본 일정 생성"
                logger.warning("schedule_ai outcome=fallback")
        preferred_candidate = available.get(preferred_place_id) if preferred_place_id else None
        meal_role = resolve_meal_role(preferred_candidate, preferred_meal_role)
        # Restaurant MAIN defaults to FLEXIBLE: probe lunch and dinner, keep the better day plan.
        if (preferred_place_id and preferred_candidate and is_restaurant(preferred_candidate)
                and meal_role == MealRole.FLEXIBLE):
            label_options: tuple[str | None, ...] = ("점심", "저녁")
        elif meal_role == MealRole.LUNCH:
            label_options = ("점심",)
        elif meal_role == MealRole.DINNER:
            label_options = ("저녁",)
        else:
            label_options = (None,)
        validation_failed = False
        supporting_cache = {}
        previous_anchor = self.return_anchor
        if end_point is not None:
            # Prefer supporting places that still leave a sensible path toward lodging / overnight base.
            self.return_anchor = end_point
        best: tuple | None = None
        try:
            for meal_label in label_options:
                for attempt in range(self.config.max_schedule_attempts):
                    items = self._build(
                        window_trip, start, activity_deadline, arrival, ordered, preferred_place_id,
                        ai_ids if attempt == 0 else [], routes,
                        max(1, self.config.max_route_calls // 2) if self.supporting_search else None,
                        preferred_meal_label=meal_label)
                    if not items:
                        break
                    base_items = items
                    items, supporting = self._fill_gaps(window_trip, window_selected, arrival, items, routes,
                                                        supporting_cache, available, exclude_ids)
                    items, supporting = self._rebalance_day_categories(
                        window_trip, window_selected, arrival, items, routes, available, supporting)
                    schedule = TripSchedule(trip_start_datetime=start, trip_end_datetime=deadline,
                                            arrival_point=arrival, items=tuple(items))
                    if supporting and not validate_schedule(schedule, window_trip, window_selected,
                                                            available | supporting, routes, arrival, self.config, set(supporting)):
                        logger.warning("supporting_schedule outcome=validation_failed fallback=base")
                        items, supporting = base_items, {}
                        schedule = schedule.model_copy(update={"items": tuple(items)})
                    if not validate_schedule(schedule, window_trip, window_selected, available | supporting,
                                             routes, arrival, self.config, set(supporting)):
                        validation_failed = True
                        logger.warning("schedule outcome=validation_failed attempt=%d", attempt + 1)
                        continue
                    if end_point is not None:
                        finished = self._append_end_point(items, end_point, routes, deadline)
                        if finished is None:
                            validation_failed = True
                            continue
                        items = finished
                        schedule = TripSchedule(trip_start_datetime=start, trip_end_datetime=deadline,
                                                arrival_point=arrival, items=tuple(items))
                    preferred_included = bool(
                        preferred_place_id and any(
                            i.place_id == preferred_place_id and i.item_type != "TRAVEL" for i in items))
                    chosen_role = meal_role
                    if preferred_included and meal_label:
                        chosen_role = MealRole.LUNCH if meal_label == "점심" else MealRole.DINNER
                    elif preferred_included and meal_role == MealRole.NONE:
                        chosen_role = MealRole.NONE
                    schedule = schedule.model_copy(update={
                        "validation_status": "VALIDATED",
                        "items": tuple(i.model_copy(update={"validation_status": "VALIDATED"}) for i in items),
                        "user_selected_place_id": preferred_place_id,
                        "anchor_status": ("INCLUDED" if preferred_included else
                                          "INFEASIBLE" if preferred_place_id else ""),
                        "anchor_meal_role": (chosen_role.value if preferred_place_id else ""),
                    })
                    notices = ["체류시간은 기본 추정값입니다. 영업시간과 실제 배차·지연은 방문 전에 확인해주세요."]
                    if preferred_place_id and not preferred_included:
                        notices.append("선택한 장소를 현재 일정 조건 안에 포함하기 어렵습니다.")
                    score = _preferred_plan_score(items, preferred_place_id or "")
                    logger.info(
                        "schedule outcome=valid visits=%d route_queries=%d attempt=%d meal_label=%s "
                        "anchor=%s meal_role=%s",
                        len([i for i in items if i.item_type != "TRAVEL"]), len(routes), attempt + 1,
                        meal_label, schedule.anchor_status, schedule.anchor_meal_role)
                    candidate = ScheduleResult(
                        "생성 완료", schedule, notices, ai_status if attempt == 0 else "기본 일정 생성")
                    if preferred_place_id:
                        if preferred_included and (best is None or score > best[0]):
                            best = (score, candidate)
                        elif best is None and not preferred_included:
                            best = (score, candidate)
                    else:
                        return candidate
                    break  # next meal_label after first valid attempt for this label
            if best is not None:
                return best[1]
        finally:
            self.return_anchor = previous_anchor
            self._role_overrides = {}
        if preferred_place_id:
            return ScheduleResult(
                "생성 완료" if not validation_failed else "일정 검증 실패",
                notices=["선택한 장소를 현재 일정 조건 안에 포함하기 어렵습니다."] + (
                    ["안전하게 검증된 일정을 만들지 못했습니다. 장소 후보나 여행 조건을 바꿔 다시 생성해주세요."]
                    if validation_failed else []))
        if validation_failed:
            return ScheduleResult("일정 검증 실패", notices=["안전하게 검증된 일정을 만들지 못했습니다. 장소 후보나 여행 조건을 바꿔 다시 생성해주세요."])
        if routes and all(route is None for route in routes.values()):
            return ScheduleResult("이동 경로 확인 불가", notices=["장소까지의 이동 경로를 확인하지 못했습니다. 잠시 후 다시 시도하거나 다른 장소를 선택해주세요."])
        return ScheduleResult("시간 조건 미충족", notices=["현재 조건에서 방문 가능한 장소 조합을 만들지 못했습니다. 선택 장소의 거리·시간대 또는 남은 시간을 확인해주세요."])

    def _append_end_point(self, items, end_point, routes, deadline):
        if not items:
            return None
        last = items[-1]
        current = None
        if last.item_type == "TRAVEL" and last.destination is not None:
            current = last.destination
        else:
            for item in reversed(items):
                if item.item_type == "TRAVEL" and item.destination and item.destination.id == last.place_id:
                    current = item.destination
                    break
            if current is None:
                current = AccessPoint(id=last.place_id, name=last.place_name,
                                      x=end_point.x, y=end_point.y)
        if current.id == end_point.id:
            return list(items)
        key = (current.id, end_point.id)
        if key not in routes:
            try:
                routes[key] = self.transit.fastest_route(current, end_point)
            except ProviderError:
                routes[key] = None
        route = routes.get(key)
        if route is None:
            return None
        start = last.end_datetime
        end = start + timedelta(seconds=route.duration_seconds)
        if end > deadline:
            return None
        travel = ScheduleItem(
            item_type="TRAVEL", place_id=end_point.id, place_name=end_point.name,
            start_datetime=start, end_datetime=end, origin=current, destination=end_point,
            travel_mode=tuple(dict.fromkeys(s.mode for s in route.steps)),
            travel_duration_minutes=route.duration_seconds / 60, reason="숙소 이동")
        return list(items) + [travel]

    def _fill_gaps(self, trip, selected, arrival, items, routes, cache, available,
                   exclude_ids: frozenset[str] = frozenset()):
        if self.supporting_search is None:
            return items, {}
        supporting = {}
        # Supporting activities may use tourism/culture/market/walk even when MAIN preference is food.
        expanded_trip = trip.model_copy(update={"preferences": tuple(dict.fromkeys(
            (*trip.preferences, Preference.REST, Preference.SIGHTSEEING,
             Preference.EXPERIENCE, Preference.SHOPPING)))})
        deadline = datetime.combine(trip.end_date, trip.end_time, KST)
        for iteration in range(self.config.max_gap_fill_iterations):
            logger.info("supporting_iteration number=%d", iteration + 1)
            iteration_route_start = len(routes)
            if sum(i.item_type != "TRAVEL" for i in items) >= self.config.max_visits:
                break
            # Collect gaps first, then fill the largest ones so a short early gap cannot block a multi-hour afternoon gap.
            gaps = []
            current, now = arrival, selected.arrival_time
            for index in range(len(items) + 1):
                following = items[index] if index < len(items) else None
                boundary = following.start_datetime if following else deadline
                minutes = (boundary - now).total_seconds() / 60
                if minutes >= self.config.min_supporting_activity_gap_minutes:
                    gaps.append((minutes, index, now, current, following, boundary))
                if following:
                    now = following.end_datetime
                    if following.item_type == "TRAVEL":
                        current = following.destination
            # Prefer long daytime gaps (before evening meal window) so afternoon fills
            # before post-dinner trailing padding; size breaks ties.
            def gap_rank(row):
                minutes, index, _now, _current, following, boundary = row
                daytime = boundary.hour < 17 or (
                    following is not None and following.start_datetime.hour <= 17)
                return (
                    0 if minutes >= self.config.long_gap_minutes else 1,
                    0 if daytime else 1,
                    0 if following is not None else 1,
                    -minutes,
                    index,
                )
            gaps.sort(key=gap_rank)
            inserted = False
            for minutes, index, now, current, following, boundary in gaps:
                logger.info("supporting_gap minutes=%.1f route_queries=%d", minutes, len(routes))
                next_point = following.destination if following and following.item_type == "TRAVEL" else None
                anchors = [current] + ([next_point] if next_point and next_point.id != current.id else [])
                if self.return_anchor and self.return_anchor.id not in {a.id for a in anchors}:
                    anchors.append(self.return_anchor)
                found = {}
                # Destination-level support pool (one Kakao search batch per destination).
                dest_key = f"dest:{trip.destination}"
                if dest_key not in cache and len(cache) < self.config.supporting_search_limit:
                    try:
                        cache[dest_key] = self.supporting_search(expanded_trip, selected, current)
                    except ProviderError:
                        cache[dest_key] = []
                        logger.warning("supporting_search outcome=failed")
                    logger.info("supporting_search candidates=%d", len(cache[dest_key]))
                for candidate in cache.get(dest_key, [])[:max(self.config.candidate_limit, 24)]:
                    found.setdefault(candidate.place_id, candidate)
                known = available | supporting
                categories = [activity_category(known[i.place_id]) for i in items
                              if i.item_type != "TRAVEL" and i.place_id in known]
                # Prior-day / excluded IDs must not re-enter via supporting (provisional hub days share a search center).
                visited = {i.place_id for i in items} | set(exclude_ids)
                used_meals = {i.meal_slot for i in items if i.meal_slot}
                options = []
                enrichment_set = {"TOURISM", "CULTURE", "WALK", "MARKET", "EXPERIENCE"}
                # Long gaps strongly prefer enrichment over cafe/food padding.
                prefer_enrichment = minutes >= self.config.long_gap_minutes
                pool = sorted(found.values(),
                              key=lambda c: (
                                  0 if activity_bucket(c) in enrichment_set else (2 if prefer_enrichment else 1),
                                  categories.count(activity_category(c)),
                                  distance_meters(current, c.latitude, c.longitude) +
                                  (distance_meters(next_point, c.latitude, c.longitude) if next_point else 0) +
                                  (distance_meters(self.return_anchor, c.latitude, c.longitude)
                                   if self.return_anchor else 0),
                                  c.place_id))
                query_start = len(routes)
                if minutes >= self.config.very_long_gap_minutes:
                    route_budget = 8
                elif minutes >= self.config.long_gap_minutes:
                    route_budget = 6
                else:
                    route_budget = 4
                prior = visited_buckets(items, known)
                user_trip = trip.model_copy(update={
                    "preferences": tuple(getattr(self, "_user_preferences", trip.preferences))})
                enrichment_left = any(
                    activity_bucket(c) in enrichment_set
                    and c.place_id not in visited
                    and c.place_id in found for c in found.values())
                for c in pool:
                    if len(routes) - query_start >= route_budget:
                        break
                    if c.place_id in visited or not c.address or not c.category or not c.place_id:
                        continue
                    preferences = [p for p in c.matched_preferences if p in expanded_trip.preferences]
                    if not preferences:
                        continue
                    preference = preferences[0]
                    role = self._role_overrides.get(c.place_id) or classify_visit_role(c, trip)
                    assessment = assess_place(c, trip, preference, self.config, meal=is_meal(c),
                                              role_override=role)
                    if not schedule_eligible(assessment, self.config, for_gap_fill=True):
                        continue
                    if not allow_cafe_candidate(c, user_trip, prior, self.config,
                                                alternatives_exist=enrichment_left,
                                                for_gap_fill=True):
                        continue
                    if not set(c.matched_preferences) & set(expanded_trip.preferences):
                        continue
                    if is_meal(c) and visit_slot(now, boundary, self.config.meal_minutes, True,
                                                Preference.FOOD, used_meals, self.config) is None:
                        continue
                    if (trip.has_pet and c.pet_status.value == "NOT_SUPPORTED") or (trip.has_child and c.child_status.value == "NOT_SUPPORTED"):
                        continue
                    visit_deadline = boundary
                    if following and following.item_type == "TRAVEL":
                        visit_deadline = following.end_datetime
                    # Destination-scoped support MAIN places use trip max distance, not nearby radius.
                    radius = activity_radius_meters(trip.activity_radius, self.extended_radius)
                    max_d = (self.config.max_distance_meters
                             if getattr(c, "role", "") == "MAIN_DESTINATION" else radius)
                    if not any(distance_meters(a, c.latitude, c.longitude) <= max_d for a in anchors):
                        continue
                    routing_candidate = c.model_copy(update={"role": "MAIN_DESTINATION"})
                    proposal = self._build(expanded_trip, now, visit_deadline, current, [routing_candidate], None, [], routes)
                    if not proposal or any(i.meal_slot in used_meals for i in proposal if i.meal_slot):
                        continue
                    end = proposal[-1].end_datetime
                    replacement = None
                    if following:
                        if following.item_type != "TRAVEL":
                            continue
                        target, point = following.destination, point_for(c)
                        key = (point.id, target.id)
                        if key not in routes and len(routes) < min(self.config.max_route_calls, query_start + route_budget):
                            try:
                                routes[key] = self.transit.fastest_route(point, target)
                            except ProviderError:
                                routes[key] = None
                        route = routes.get(key)
                        if route is None or end + timedelta(seconds=route.duration_seconds,
                                minutes=self.config.gap_safety_buffer_minutes) > following.end_datetime:
                            continue
                        replacement = following.model_copy(update={"origin": point,
                            "start_datetime": following.end_datetime - timedelta(seconds=route.duration_seconds),
                            "travel_duration_minutes": route.duration_seconds / 60,
                            "travel_mode": tuple(dict.fromkeys(s.mode for s in route.steps))})
                    elif self.return_anchor is not None:
                        # Final-day trailing gap: candidate must still reach return hub before cutoff.
                        point = point_for(c)
                        if point.id != self.return_anchor.id:
                            key = (point.id, self.return_anchor.id)
                            if key not in routes and len(routes) < min(self.config.max_route_calls, query_start + route_budget):
                                try:
                                    routes[key] = self.transit.fastest_route(point, self.return_anchor)
                                except ProviderError:
                                    routes[key] = None
                            hub_route = routes.get(key)
                            if hub_route is None:
                                continue
                            if end + timedelta(seconds=hub_route.duration_seconds,
                                    minutes=self.config.gap_safety_buffer_minutes) > deadline:
                                continue
                    cost = sum(i.duration_minutes for i in proposal if i.item_type == "TRAVEL")
                    cost += replacement.duration_minutes if replacement else 0
                    direct = following.duration_minutes if next_point else 0
                    detour = max(0, cost - direct)
                    div = diversity_penalty(c, user_trip, prior, self.config)
                    # Prefer enrichment categories and high suitability; avoid cafe stacking.
                    options.append((div - assessment.suitability_score,
                                    categories.count(activity_category(c)),
                                    cost + detour, c.place_id, c, proposal, replacement))
                if options:
                    for _, _, _, _, c, proposal, replacement in sorted(options, key=lambda o: o[:4]):
                        proposed = items[:index] + proposal + ([replacement] if replacement else []) + items[index + (1 if replacement else 0):]
                        trial = TripSchedule(trip_start_datetime=selected.arrival_time, trip_end_datetime=deadline,
                                             arrival_point=arrival, items=tuple(proposed))
                        sources = supporting | {c.place_id: c}
                        if not validate_schedule(trial, trip, selected, available | sources, routes,
                                                 arrival, self.config, set(sources)):
                            logger.warning("supporting_insert outcome=rollback")
                            continue
                        supporting, items, inserted = sources, proposed, True
                        break
                    if inserted:
                        break
                logger.info("supporting_gap outcome=free_time candidates=%d route_queries=%d", len(found), len(routes))
            if not inserted:
                if len(routes) > iteration_route_start and len(routes) < self.config.max_route_calls:
                    continue
                break
        return items, supporting

    def _rebalance_day_categories(self, trip, selected, arrival, items, routes, available, supporting):
        """If the day is cafe-stacked and enrichment candidates exist, swap one cafe block."""
        known = dict(available) | dict(supporting)
        buckets = visited_buckets(items, known)
        user_trip = trip.model_copy(update={
            "preferences": tuple(getattr(self, "_user_preferences", trip.preferences))})
        if not cafe_imbalanced(buckets, user_trip, self.config):
            return items, supporting
        enrichment = [c for c in (list(available.values()) + list(supporting.values()))
                      if activity_bucket(c) in {"TOURISM", "CULTURE", "WALK", "MARKET", "EXPERIENCE"}
                      and not is_micro_landmark(c)
                      and c.place_id not in {i.place_id for i in items}]
        if not enrichment:
            logger.info("day_quality outcome=cafe_imbalance no_enrichment_candidate")
            return items, supporting
        # Find a non-meal CAFE place item to replace (prefer the last consecutive cafe).
        cafe_indexes = [idx for idx, item in enumerate(items)
                        if item.item_type != "TRAVEL" and known.get(item.place_id)
                        and activity_bucket(known[item.place_id]) in {"CAFE", "REST"}]
        if len(cafe_indexes) < 2:
            return items, supporting
        target_idx = cafe_indexes[-1]
        cafe_item = items[target_idx]
        # Preceding travel (if any) and following travel stay; rebuild segment around cafe slot.
        start_idx = target_idx - 1 if target_idx > 0 and items[target_idx - 1].item_type == "TRAVEL" else target_idx
        end_idx = target_idx
        prev_end = items[start_idx - 1].end_datetime if start_idx > 0 else selected.arrival_time
        origin = arrival
        if start_idx > 0:
            prev = items[start_idx - 1]
            if prev.item_type == "TRAVEL" and prev.destination:
                origin = prev.destination
            elif prev.item_type != "TRAVEL":
                origin = point_for(known[prev.place_id]) if prev.place_id in known else arrival
        following = items[end_idx + 1] if end_idx + 1 < len(items) else None
        boundary = following.start_datetime if following else datetime.combine(trip.end_date, trip.end_time, KST)
        for candidate in sorted(enrichment, key=lambda c: -suitability_score(c, user_trip)):
            role = self._role_overrides.get(candidate.place_id) or classify_visit_role(candidate, trip)
            pref = next((p for p in candidate.matched_preferences
                         if p in (Preference.SIGHTSEEING, Preference.EXPERIENCE, Preference.SHOPPING, Preference.REST)),
                        Preference.SIGHTSEEING)
            assessment = assess_place(candidate, trip, pref, self.config, role_override=role)
            if not schedule_eligible(assessment, self.config, for_gap_fill=True):
                continue
            proposal = self._build(
                trip.model_copy(update={"preferences": tuple(dict.fromkeys(
                    (*trip.preferences, Preference.REST, Preference.SIGHTSEEING, Preference.EXPERIENCE)))}),
                prev_end, boundary, origin, [candidate.model_copy(update={"role": "MAIN_DESTINATION"})],
                None, [], routes)
            if not proposal:
                continue
            # Keep following travel usable when it existed.
            replacement = None
            new_tail_start = end_idx + 1
            if following and following.item_type == "TRAVEL":
                last = proposal[-1]
                point = None
                if last.item_type == "TRAVEL" and last.destination:
                    point = last.destination
                else:
                    point = point_for(candidate)
                key = (point.id, following.destination.id)
                if key not in routes:
                    try:
                        routes[key] = self.transit.fastest_route(point, following.destination)
                    except ProviderError:
                        routes[key] = None
                route = routes.get(key)
                if route is None:
                    continue
                arrive = following.end_datetime
                depart = arrive - timedelta(seconds=route.duration_seconds)
                if proposal[-1].end_datetime > depart:
                    continue
                replacement = following.model_copy(update={
                    "origin": point, "start_datetime": depart,
                    "travel_duration_minutes": route.duration_seconds / 60,
                    "travel_mode": tuple(dict.fromkeys(s.mode for s in route.steps))})
                new_tail_start = end_idx + 2
            proposed = list(items[:start_idx]) + proposal + ([replacement] if replacement else []) + list(items[new_tail_start:])
            trial = TripSchedule(trip_start_datetime=selected.arrival_time,
                                 trip_end_datetime=datetime.combine(trip.end_date, trip.end_time, KST),
                                 arrival_point=arrival, items=tuple(proposed))
            sources = supporting | {candidate.place_id: candidate}
            if not validate_schedule(trial, trip, selected, available | sources, routes,
                                     arrival, self.config, set(sources)):
                continue
            logger.info("day_quality outcome=rebalanced replaced_cafe=%s with=%s",
                        cafe_item.place_name, candidate.place_name)
            return proposed, sources
        logger.info("day_quality outcome=cafe_imbalance retained")
        return items, supporting

    def _build(self, trip, start, deadline, arrival, candidates, preferred_id, ai_ids, routes, route_limit=None,
               preferred_meal_label: str | None = None):
        current, now = arrival, start
        items, visited, meals, counts = [], set(), set(), {}
        radius = activity_radius_meters(trip.activity_radius, self.extended_radius)
        # Original user preferences (expanded supporting trips still carry REST/SIGHTSEEING extras).
        user_prefs = set(getattr(self, "_user_preferences", trip.preferences) or trip.preferences)
        known = {c.place_id: c for c in candidates}
        for _ in range(self.config.max_visits):
            prior = visited_buckets(items, known)
            enrichment_left = any(
                activity_bucket(c) in {"TOURISM", "CULTURE", "WALK", "MARKET", "EXPERIENCE"}
                and c.place_id not in visited for c in candidates)
            choices = []
            for c in candidates:
                if c.place_id in visited:
                    continue
                distance = distance_meters(current, c.latitude, c.longitude)
                if (distance > self.config.max_distance_meters and c.place_id != preferred_id) or (c.role == "NEARBY_PLACE" and distance > radius):
                    continue
                preferences = [p for p in c.matched_preferences if p in trip.preferences]
                if not preferences:
                    continue
                preference = min(preferences, key=lambda p: counts.get(p, 0))
                role = self._role_overrides.get(c.place_id)
                assessment = assess_place(c, trip, preference, self.config, meal=is_meal(c),
                                          role_override=role)
                if not schedule_eligible(assessment, self.config,
                                         preferred=c.place_id == preferred_id):
                    continue
                # Diversity uses the traveler's stated prefs, not the expanded supporting set.
                diversity_trip = trip.model_copy(update={"preferences": tuple(user_prefs)}) if user_prefs else trip
                if not allow_cafe_candidate(c, diversity_trip, prior, self.config,
                                            alternatives_exist=enrichment_left):
                    continue
                score = (assessment.suitability_score
                         + c.preference_score * 0.25
                         + 15 / (1 + counts.get(preference, 0)))
                score += max(0, 10 - ai_ids.index(c.place_id)) if c.place_id in ai_ids else 0
                score -= distance / 1000
                score -= diversity_penalty(c, diversity_trip, prior, self.config)
                choices.append((c.place_id == preferred_id, score, c, preference, assessment))
            choices.sort(key=lambda v: (-v[0], -v[1], v[2].place_id))
            feasible = []
            for preferred, score, c, preference, assessment in choices:
                duration = assessment.duration_minutes
                allowed = None
                if (preferred_meal_label and preferred_id and c.place_id == preferred_id
                        and is_meal(c)):
                    allowed = (preferred_meal_label,)
                # A filled meal window cannot become feasible by adding travel time.
                if visit_slot(now, deadline, duration,
                              is_meal(c), preference, meals, self.config,
                              allowed_meal_labels=allowed) is None:
                    continue
                target = point_for(c)
                key = (current.id, target.id)
                route = None
                if current.id != target.id:
                    if key not in routes:
                        if len(routes) >= (route_limit or self.config.max_route_calls):
                            continue
                        try:
                            routes[key] = self.transit.fastest_route(current, target)
                        except ProviderError:
                            routes[key] = None
                    route = routes[key]
                    if route is None:
                        continue
                seconds = route.duration_seconds if route else 0
                slot = visit_slot(now + timedelta(seconds=seconds), deadline,
                                  duration, is_meal(c), preference, meals, self.config,
                                  allowed_meal_labels=allowed)
                if slot is None:
                    continue
                # Itinerary suitability + preference relevance − route/detour waiting − diversity.
                waiting_minutes = max(0, (slot[0] - now).total_seconds() / 60 - seconds / 60)
                utility = score - seconds / 60 - waiting_minutes / 6
                # FLEXIBLE restaurant MAIN: slightly prefer the forced label's natural window fit.
                if preferred and allowed:
                    utility += 2
                feasible.append((preferred, utility, c, preference, target, route, slot, assessment))
                if preferred or len(feasible) >= self.config.shortlist_limit:
                    break
            if not feasible:
                break
            preferred, _, c, preference, target, route, (visit_start, visit_end, meal_slot), assessment = max(
                feasible, key=lambda v: (v[0], v[1]))
            if route:
                items.append(ScheduleItem(item_type="TRAVEL", place_id=c.place_id, place_name=c.place_name,
                    start_datetime=visit_start - timedelta(seconds=route.duration_seconds), end_datetime=visit_start,
                    origin=current, destination=target, travel_mode=tuple(dict.fromkeys(s.mode for s in route.steps)),
                    travel_duration_minutes=route.duration_seconds / 60, reason="경로 API 예상 이동시간"))
            checks = ["영업시간 확인 필요"]
            if trip.allergies and is_meal(c):
                checks.append("알레르기 정보 확인 필요")
            if trip.has_pet:
                checks.append("반려동물 동반 가능 여부 확인 필요")
            if trip.has_child:
                checks.append("아이 동반 이용 조건 확인 필요")
            items.append(ScheduleItem(item_type="MEAL" if is_meal(c) else "PLACE", place_id=c.place_id,
                place_name=c.place_name, start_datetime=visit_start, end_datetime=visit_end,
                estimated_duration=True, preference=preference.value, meal_slot=meal_slot,
                reason=(meal_slot.split(":", 1)[1] + " · " if meal_slot else "") + c.category + " · " + preference.value + " 성향 후보",
                checks=tuple(checks)))
            if meal_slot:
                meals.add(meal_slot)
            visited.add(c.place_id)
            known[c.place_id] = c
            counts[preference] = counts.get(preference, 0) + 1
            current, now = target, visit_end
        return items


def generate_trip_schedule(trip: TripRequest, selected: TransportCandidate | None,
                           candidates: list[PlaceCandidate], settings: Settings,
                           preferred_place_id: str | None = None,
                           accommodation_query: str | None = None, *,
                           accommodation_undecided: bool = False,
                           accommodation_point=None) -> ScheduleResult:
    http = HttpClient(max_attempts=2)
    try:
        kakao = KakaoProvider(settings.kakao_rest_api_key, http)
        transit = KakaoTransitProvider(settings.kakao_rest_api_key, http)
        resolver = AccessService(kakao, transit)
        groq = GroqProvider(settings.groq_api_key, http, settings.groq_model) if settings.groq_api_key else None
        def supporting_search(trip, selected, point):
            from services.schedule_support_search import search_schedule_support
            # Completely separate from MAIN preference ranking — destination enrichment pool.
            return search_schedule_support(
                kakao, trip, point,
                exclude_ids=frozenset(),
                include_cafe=True,
                limit=24)
        from services.return_schedule_service import ReturnScheduleService
        from services.transport_service import TransportService
        from providers.tago_train_provider import TagoTrainProvider
        from providers.tago_bus_provider import TagoBusProvider
        transport = TransportService(TagoTrainProvider(settings.data_go_kr_api_key, http),
            TagoBusProvider(settings.data_go_kr_api_key, http, "express"),
            TagoBusProvider(settings.data_go_kr_api_key, http, "intercity"), kakao,
            local_origin_hub_scan_limit=settings.local_origin_hub_scan_limit,
            local_origin_hub_limit=settings.local_origin_hub_limit,
            access_service=resolver)
        activity = ScheduleService(transit, resolver.resolve_point, config=settings.schedule,
            supporting_search=supporting_search, extended_radius=settings.extended_activity_radius_meters,
            groq=groq)
        return ReturnScheduleService(activity, transport, resolver).generate(
            trip, selected, candidates, preferred_place_id, accommodation_query,
            accommodation_undecided=accommodation_undecided,
            accommodation_point=accommodation_point)
    finally:
        http.close()


def resolve_trip_accommodation(query: str, settings: Settings):
    """UI lodging resolve; returns (AccessPoint, None) or (None, error_message)."""
    from services.multiday_schedule_service import resolve_accommodation, accommodation_error_message
    http = HttpClient(max_attempts=2)
    try:
        kakao = KakaoProvider(settings.kakao_rest_api_key, http)
        transit = KakaoTransitProvider(settings.kakao_rest_api_key, http)
        resolver = AccessService(kakao, transit)
        try:
            return resolve_accommodation(resolver, query), None
        except ProviderError as exc:
            return None, accommodation_error_message(exc.code)
    finally:
        http.close()
