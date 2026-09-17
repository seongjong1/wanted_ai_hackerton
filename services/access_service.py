"""Resolve precise places, then check access + buffer before boarding."""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from models.access import AccessLeg, AccessPoint, AccessRoute, AccessStep, BoardingAssessment
from models.transport import KST, TransportCandidate, TransportType
from models.trip_request import TripRequest
from providers.http_client import ProviderError
from providers.kakao_provider import KakaoProvider
from providers.kakao_transit_provider import KakaoTransitProvider
from services.location_service import normalize

logger = logging.getLogger("travel_ai.access")

# When Kakao publictraffic is unavailable (quota/outage), keep outbound alive with a
# conservative urban-transit estimate from straight-line distance (~18 km/h effective).
_ESTIMATE_METERS_PER_SECOND = 5.0
_ESTIMATE_MIN_SECONDS = 300.0


@dataclass
class AccessCache:
    legs: dict[tuple, AccessLeg | None] = field(default_factory=dict)
    points: dict[str, AccessPoint | None] = field(default_factory=dict)
    routes: dict[tuple[str, str], AccessRoute | None] = field(default_factory=dict)


class AccessService:
    def __init__(self, places: KakaoProvider, transit: KakaoTransitProvider,
                 *, train_buffer_minutes: float = 15, bus_buffer_minutes: float = 15) -> None:
        import math
        if any(not math.isfinite(v) or v < 0 for v in (train_buffer_minutes, bus_buffer_minutes)):
            raise ValueError("Invalid boarding buffer")
        self.places = places
        self.transit = transit
        self.train_buffer_minutes = train_buffer_minutes
        self.bus_buffer_minutes = bus_buffer_minutes

    def filter_candidates(self, candidates: list[TransportCandidate], trip: TripRequest,
                          cache: AccessCache) -> tuple[list[TransportCandidate], int, int]:
        accepted = []
        infeasible = unknown = 0
        for candidate in candidates:
            query = self.hub_query(candidate)
            departure = datetime.combine(trip.start_date, trip.departure_time, KST)
            key = (trip.departure, query, departure)
            if key not in cache.legs:
                try:
                    cache.legs[key] = self.get_leg(trip.departure, query, departure, cache)
                except ProviderError:
                    cache.legs[key] = None
            access = cache.legs[key]
            if access is None:
                unknown += 1
                continue
            buffer = (self.train_buffer_minutes if candidate.transport_type == TransportType.TRAIN
                      else self.bus_buffer_minutes)
            assessment = BoardingAssessment(access, buffer, candidate.departure_time, candidate.arrival_time)
            if not assessment.is_feasible:
                infeasible += 1
                continue
            accepted.append(candidate.model_copy(update={"access": assessment}))
        logger.info(
            "access_filter raw=%d accepted=%d infeasible=%d unknown=%d",
            len(candidates), len(accepted), infeasible, unknown)
        return accepted, infeasible, unknown

    @staticmethod
    def hub_query(candidate: TransportCandidate) -> str:
        name = candidate.departure_place
        if candidate.transport_type == TransportType.TRAIN:
            return name if name.endswith("역") else name + "역"
        if "터미널" in name:
            return name
        return name + "버스터미널"

    def resolve_origin(self, query: str, address: str | None = None) -> AccessPoint:
        from services.origin_service import OriginResolver
        resolver = OriginResolver(self.places)
        return resolver.resolve_address(address, original_query=query) if address is not None else resolver.resolve(query)

    def resolve_point(self, query: str) -> AccessPoint:
        documents = self.places.search_places(query, size=15)
        matches: dict[str, AccessPoint] = {}
        exact_matches: dict[str, AccessPoint] = {}
        requested = normalize(query)
        for row in documents:
            try:
                point = AccessPoint(id=row["id"], name=row["place_name"], x=row["x"], y=row["y"],
                                    address=row.get("address_name", ""), category=row.get("category_name", ""))
            except (KeyError, TypeError, ValidationError):
                continue
            # Allow descriptive rail-line suffixes, but never choose a shop or a city centroid.
            canonical = normalize(re.sub(r"\([^)]*\)", "", point.name))
            name = normalize(point.name)
            if requested == name or requested == canonical or requested == normalize(point.address):
                exact_matches[point.id] = point
                matches[point.id] = point
            elif "터미널" in requested and "버스터미널" in row.get("category_name", ""):
                terminal_key = lambda text: re.sub(r"고속|시외|종합|버스|터미널", "", text)
                if terminal_key(requested) == terminal_key(name):
                    matches[point.id] = point
            elif requested.endswith("역") and name.startswith(requested) and any(kind in row.get("category_name", "") for kind in ("기차", "지하철")):
                matches[point.id] = point
        # Exact POIs outrank line-suffixed subway POIs returned for the same station.
        # Keep genuine ambiguity (multiple exact POIs) unresolved.
        if exact_matches:
            matches = exact_matches
        if len(matches) != 1:
            raise ProviderError("ACCESS_TIME_UNKNOWN")
        return next(iter(matches.values()))

    @staticmethod
    def _estimate_route(origin: AccessPoint, destination: AccessPoint) -> AccessRoute:
        from services.place_service import distance_meters
        meters = distance_meters(origin, destination.y, destination.x)
        seconds = max(_ESTIMATE_MIN_SECONDS, meters / _ESTIMATE_METERS_PER_SECOND)
        return AccessRoute(
            duration_seconds=seconds,
            distance_meters=meters,
            transfers=0,
            steps=(AccessStep(
                mode="ESTIMATED",
                duration_seconds=seconds,
                distance_meters=meters,
                guidance="직선거리 기반 추정 · 실제 대중교통 경로 아님",
            ),),
        )

    def get_leg(self, origin_query: str, destination_query: str, departure: datetime,
                cache: AccessCache | None = None) -> AccessLeg:
        cache = cache if cache is not None else AccessCache()
        for query in (origin_query, destination_query):
            if query not in cache.points:
                try:
                    cache.points[query] = self.resolve_point(query)
                except ProviderError:
                    cache.points[query] = None
            if cache.points[query] is None:
                raise ProviderError("ACCESS_TIME_UNKNOWN")
        origin, destination = cache.points[origin_query], cache.points[destination_query]
        if origin.id == destination.id:
            # Same verified POI: no inter-place movement; platform access stays in the buffer.
            return AccessLeg(origin=origin.name, destination=destination.name,
                             transport_modes=("SAME_PLACE",), duration_minutes=0, distance_meters=0,
                             departure_time=departure, provider="Kakao Local · same place ID",
                             origin_point=origin, destination_point=destination,
                             note="동일 장소 ID 확인. 승강장/승차장 이동은 승차 버퍼에 포함합니다.")
        route_key = (origin.id, destination.id)
        estimated = False
        if route_key not in cache.routes:
            try:
                cache.routes[route_key] = self.transit.fastest_route(origin, destination)
            except ProviderError as exc:
                # Outbound must not collapse to zero when Mobility routing is down/quota-limited.
                # Place resolution already succeeded; use a conservative distance estimate.
                logger.warning("access_route outcome=estimate reason=%s", exc.code)
                cache.routes[route_key] = self._estimate_route(origin, destination)
                estimated = True
        route = cache.routes[route_key]
        if route is None:
            raise ProviderError("ACCESS_TIME_UNKNOWN")
        # Re-detect estimate marker when reading a cached estimate route.
        if not estimated and route.steps and route.steps[0].mode == "ESTIMATED":
            estimated = True
        return AccessLeg(origin=origin.name, destination=destination.name,
                         transport_modes=tuple(dict.fromkeys(s.mode for s in route.steps)),
                         duration_minutes=route.duration_seconds / 60,
                         distance_meters=route.distance_meters, transfers=route.transfers,
                         steps=route.steps, departure_time=departure,
                         provider=("ESTIMATED" if estimated else "Kakao publictraffic"),
                         origin_point=origin, destination_point=destination,
                         estimated=estimated,
                         note=("경로 API를 확인하지 못해 직선거리 기반 추정 이동시간을 사용합니다. 실제 대중교통 경로가 아닙니다."
                               if estimated else
                               "API 예상 소요시간 기준. 지정 날짜/시각의 배차·실시간 지연은 보장하지 않습니다."))
