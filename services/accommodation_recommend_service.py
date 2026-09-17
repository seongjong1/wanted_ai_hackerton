"""Recommend real Kakao lodging POIs for undecided multi-day trips (Phase 4.8).

Never invents prices, ratings, rooms, or availability. Caps Kakao publictraffic
calls via distance pre-filter + shared route cache.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config import ScheduleSettings, Settings
from models.access import AccessPoint
from models.accommodation import AccommodationCandidate, AccommodationRecommendResult
from models.place import PlaceCandidate
from models.schedule import TripSchedule
from models.transport import TransportCandidate
from models.trip_request import TripRequest
from providers.http_client import HttpClient, ProviderError
from providers.kakao_provider import KakaoProvider
from providers.kakao_transit_provider import KakaoTransitProvider
from services.access_service import AccessService
from services.multiday_schedule_service import arrival_hub_query
from services.place_service import belongs_to_destination, distance_meters

logger = logging.getLogger("travel_ai.accommodation_recommend")

LODGING_CATEGORY_KEYS = ("숙박", "호텔", "모텔", "리조트", "게스트하우스", "펜션", "민박")


def is_lodging_category(category: str) -> bool:
    return any(key in (category or "") for key in LODGING_CATEGORY_KEYS)


def _hub_label(selected: TransportCandidate) -> str:
    return arrival_hub_query(selected)


def _reason_for(candidate: AccommodationCandidate, hub_name: str) -> str:
    if candidate.route_to_hub_minutes is not None:
        return (f"{hub_name} 접근이 비교적 좋은 위치의 후보입니다. "
                f"(대중교통 약 {candidate.route_to_hub_minutes:.0f}분)")
    if candidate.distance_to_hub_meters is not None:
        km = candidate.distance_to_hub_meters / 1000
        if km < 1.5:
            return "현재 일정 기준 숙소 이동 거리가 짧은 후보입니다."
        return "여행 동선상 이동 부담이 적은 후보입니다."
    return "여행 동선과 위치를 기준으로 찾은 숙소 후보입니다."


def _anchors_from_schedule(schedule: TripSchedule | None,
                           hub: AccessPoint,
                           preferred: PlaceCandidate | None) -> list[AccessPoint]:
    """Lightweight itinerary anchors for lodging ranking — no extra API calls."""
    anchors = [hub]
    if preferred is not None:
        anchors.append(AccessPoint(
            id=preferred.place_id, name=preferred.place_name,
            x=preferred.longitude, y=preferred.latitude,
            address=preferred.address, category=preferred.category, source="place"))
    if schedule and schedule.days:
        for day in schedule.days:
            if day.role == "FINAL":
                continue
            # Last non-travel place before overnight return approximates DAY-N end activity.
            for item in reversed(day.items):
                if item.item_type != "TRAVEL" and item.destination is None:
                    # Place visits keep coords only via place_id; prefer travel destination into lodging.
                    break
                if item.item_type == "TRAVEL" and item.destination is not None:
                    if item.destination.id not in {a.id for a in anchors}:
                        anchors.append(item.destination)
                    break
            if day.role == "FIRST" and day.end_location.id not in {a.id for a in anchors}:
                # Before lodging confirm, end_location may still be provisional hub.
                pass
    return anchors


def _straight_score(point: AccessPoint, anchors: list[AccessPoint]) -> float:
    """Lower is better — sum of haversine meters to itinerary anchors."""
    total = 0.0
    for anchor in anchors:
        total += distance_meters(anchor, point.y, point.x)
    return total


@dataclass
class AccommodationRecommendService:
    kakao: KakaoProvider
    transit: KakaoTransitProvider | None
    access: AccessService
    config: ScheduleSettings

    def search(self, trip: TripRequest, selected: TransportCandidate,
               *, preferred: PlaceCandidate | None = None,
               schedule: TripSchedule | None = None,
               place_pool: list[PlaceCandidate] | None = None,
               ) -> AccommodationRecommendResult:
        notices: list[str] = []
        try:
            hub = self.access.resolve_point(_hub_label(selected))
        except ProviderError:
            logger.warning("accommodation_recommend outcome=hub_unresolved")
            return AccommodationRecommendResult(
                status="failed",
                notices=("숙소 후보를 불러오지 못했습니다. "
                         "숙소명을 직접 입력하거나 임시 기준점으로 일정을 생성할 수 있습니다.",))

        found: dict[str, AccommodationCandidate] = {}
        search_count = 0
        failed = 0
        for term in self.config.accommodation_search_queries:
            query = f"{trip.destination} {term}"
            for page in range(1, self.config.accommodation_search_pages + 1):
                try:
                    rows = self.kakao.search_places(
                        query, page=page, size=self.config.accommodation_search_size)
                    search_count += 1
                except ProviderError:
                    failed += 1
                    logger.warning("accommodation_search outcome=failed query=%s", term)
                    break
                for row in rows:
                    address = row.get("road_address_name") or row.get("address_name") or ""
                    if not isinstance(address, str) or not belongs_to_destination(
                            trip.destination, address):
                        continue
                    category = row.get("category_name") or ""
                    if not is_lodging_category(category):
                        continue
                    try:
                        pid = str(row["id"])
                        name = str(row["place_name"])
                        x = float(row["x"])
                        y = float(row["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if pid in found:
                        continue
                    dist = distance_meters(hub, y, x)
                    found[pid] = AccommodationCandidate(
                        place_id=pid, place_name=name, address=address, category=category,
                        latitude=y, longitude=x, distance_to_hub_meters=dist)

        if not found:
            status = "failed" if failed else "empty"
            message = (
                "숙소 후보를 불러오지 못했습니다. "
                "숙소명을 직접 입력하거나 임시 기준점으로 일정을 생성할 수 있습니다."
                if status == "failed" else
                "근처에서 숙박업소 후보를 찾지 못했습니다. "
                "숙소명을 직접 입력하거나 임시 기준점으로 일정을 생성할 수 있습니다.")
            return AccommodationRecommendResult(
                status=status, notices=(message,), search_count=search_count)

        # Prefer MAIN / place-pool centroid as secondary anchors when available.
        anchors = _anchors_from_schedule(schedule, hub, preferred)
        if preferred is None and place_pool:
            mains = [c for c in place_pool if c.role == "MAIN_DESTINATION"][:3]
            for c in mains:
                anchors.append(AccessPoint(
                    id=c.place_id, name=c.place_name, x=c.longitude, y=c.latitude,
                    address=c.address, category=c.category, source="place"))

        ranked = sorted(
            found.values(),
            key=lambda c: (_straight_score(
                AccessPoint(id=c.place_id, name=c.place_name, x=c.longitude, y=c.latitude),
                anchors), c.distance_to_hub_meters or 0, c.place_id))
        shortlist = ranked[:self.config.accommodation_prefilter_limit]

        # Route-eval only a tiny top set; reuse Kakao transit process cache.
        route_eval = 0
        scored: list[AccommodationCandidate] = []
        for candidate in shortlist[:self.config.accommodation_route_eval_limit]:
            point = candidate.as_access_point()
            route_minutes = None
            if self.transit is not None:
                try:
                    route = self.transit.fastest_route(hub, point)
                    route_minutes = route.duration_seconds / 60
                    route_eval += 1
                except ProviderError:
                    pass
            straight = _straight_score(point, anchors)
            # Prefer real route minutes when present; fall back to haversine meters.
            score = (route_minutes * 1000 if route_minutes is not None else straight)
            updated = candidate.model_copy(update={
                "route_to_hub_minutes": route_minutes,
                "score": score,
            })
            scored.append(updated.model_copy(update={
                "reason": _reason_for(updated, hub.name)}))

        scored.sort(key=lambda c: (c.score, c.distance_to_hub_meters or 0, c.place_id))
        final = tuple(scored[:self.config.accommodation_recommend_limit])
        notices.append(
            "여행 동선과 위치를 기준으로 찾은 숙소입니다. "
            "객실 가격과 예약 가능 여부는 숙박 예약 서비스에서 확인해주세요.")
        logger.info(
            "accommodation_recommend outcome=ok search=%d prefilter=%d route_eval=%d final=%d",
            search_count, len(shortlist), route_eval, len(final))
        return AccommodationRecommendResult(
            status="ok", candidates=final, notices=tuple(notices),
            search_count=search_count, route_eval_count=route_eval)


def search_accommodation_candidates(
        trip: TripRequest, selected: TransportCandidate, settings: Settings, *,
        preferred: PlaceCandidate | None = None,
        schedule: TripSchedule | None = None,
        place_pool: list[PlaceCandidate] | None = None,
) -> AccommodationRecommendResult:
    """App entry — lodging recommend failures never affect outbound/schedule layers."""
    http = HttpClient(max_attempts=1)
    try:
        kakao = KakaoProvider(settings.kakao_rest_api_key, http)
        transit = KakaoTransitProvider(settings.kakao_rest_api_key, http)
        access = AccessService(kakao, transit)
        service = AccommodationRecommendService(
            kakao, transit, access, settings.schedule)
        return service.search(
            trip, selected, preferred=preferred, schedule=schedule, place_pool=place_pool)
    except ProviderError:
        logger.warning("accommodation_recommend outcome=provider_error")
        return AccommodationRecommendResult(
            status="failed",
            notices=("숙소 후보를 불러오지 못했습니다. "
                     "숙소명을 직접 입력하거나 임시 기준점으로 일정을 생성할 수 있습니다.",))
    finally:
        http.close()
