"""Combine real schedules with deterministic filtering and isolated failures."""
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, Callable

from pydantic import ValidationError

from config import Settings
from models.transport import (KST, TransportCandidate, TransportResult, TransportStatus,
                              TransportType, HubMatch)
from models.trip_request import TripRequest
from providers.http_client import HttpClient, ProviderError
from providers.kakao_provider import KakaoProvider
from providers.tago_bus_provider import TagoBusProvider
from providers.tago_train_provider import TagoTrainProvider
from services.health_service import describe_error
from services.location_service import HubResolver, LocationContext, resolve_context, text_field
from services.access_service import AccessService, AccessCache
from providers.kakao_transit_provider import KakaoTransitProvider

logger = logging.getLogger("travel_ai.transport")


def parse_tago_time(value: str) -> datetime:
    """Accept documented date+time formats only; never infer a day or duration."""
    formats = {12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
    if not value.isascii() or not value.isdigit() or len(value) not in formats:
        raise ValueError("Invalid TAGO datetime")
    return datetime.strptime(value, formats[len(value)]).replace(tzinfo=KST)


def convert_candidate(row: dict[str, Any], kind: TransportType) -> TransportCandidate:
    train = kind == TransportType.TRAIN
    raw_price = text_field(row, "adultcharge" if train else "charge")
    price = None
    if raw_price:
        try:
            parsed = Decimal(raw_price)
            if parsed.is_finite() and parsed >= 0:
                price = parsed
        except InvalidOperation:
            pass  # Optional malformed fare is explicitly unknown, never zero.
    return TransportCandidate(
        transport_type=kind,
        departure_place=text_field(row, "depplacename" if train else "depPlaceNm"),
        arrival_place=text_field(row, "arrplacename" if train else "arrPlaceNm"),
        departure_time=parse_tago_time(text_field(row, "depplandtime")),
        arrival_time=parse_tago_time(text_field(row, "arrplandtime")),
        price=price, grade=text_field(row, "traingradename" if train else "gradeNm") or None,
        route_id=text_field(row, "routeId") or None,
        train_number=text_field(row, "trainno") or None,
        provider={TransportType.TRAIN: "TAGO TrainInfo", TransportType.EXPRESS_BUS: "TAGO ExpBusInfo",
                  TransportType.INTERCITY_BUS: "TAGO SuburbsBusInfo"}[kind],
        raw_data=row.copy(),
    )


def rank_candidates(candidates: list[TransportCandidate], trip: TripRequest) -> list[TransportCandidate]:
    """Outbound ranking only. end_time is a home-return goal, not an outbound arrival cutoff."""
    earliest = datetime.combine(trip.start_date, trip.departure_time, KST)
    unique: dict[tuple, TransportCandidate] = {}
    for candidate in candidates:
        if candidate.access is not None and not candidate.access.is_feasible:
            continue
        if candidate.departure_time.date() != trip.start_date or candidate.departure_time < earliest:
            continue
        # Keep same-trip-window arrivals; do not use TripRequest.end_time here.
        if candidate.arrival_time.date() > trip.end_date:
            continue
        key = (candidate.transport_type, candidate.departure_place, candidate.arrival_place,
               candidate.departure_time, candidate.arrival_time, candidate.grade,
               candidate.train_number, candidate.route_id)
        previous = unique.get(key)
        if previous is None or (candidate.price is not None and
                                (previous.price is None or candidate.price < previous.price)):
            unique[key] = candidate
    return sorted(unique.values(), key=lambda c: (
        c.arrival_time, c.access.total_duration_minutes if c.access else c.duration_minutes,
        c.price if c.price is not None else Decimal("Infinity"),
        c.transport_type.value, c.departure_place, c.arrival_place, c.grade or "",
        c.train_number or "", c.route_id or ""))


def rank_return_candidates(candidates: list[TransportCandidate], trip: TripRequest,
                           *, earliest_departure: datetime) -> list[TransportCandidate]:
    """Return-mode ranking. Hub arrival after end_time is only a soft prefilter."""
    latest_hub = datetime.combine(trip.end_date, trip.end_time, KST)
    unique: dict[tuple, TransportCandidate] = {}
    for candidate in candidates:
        if candidate.departure_time.date() != trip.end_date or candidate.departure_time < earliest_departure:
            continue
        if candidate.arrival_time > latest_hub:
            continue
        key = (candidate.transport_type, candidate.departure_place, candidate.arrival_place,
               candidate.departure_time, candidate.arrival_time, candidate.grade,
               candidate.train_number, candidate.route_id)
        previous = unique.get(key)
        if previous is None or (candidate.price is not None and
                                (previous.price is None or candidate.price < previous.price)):
            unique[key] = candidate
    return sorted(unique.values(), key=lambda c: (
        c.arrival_time, c.duration_minutes,
        c.price if c.price is not None else Decimal("Infinity"),
        c.transport_type.value, c.departure_place, c.arrival_place, c.grade or "",
        c.train_number or "", c.route_id or ""))


class TransportService:
    def __init__(self, train: TagoTrainProvider, express: TagoBusProvider,
                 intercity: TagoBusProvider, kakao: KakaoProvider | None = None,
                 *, max_hubs: int = 3, per_type_limit: int = 5, total_limit: int = 5,
                 today: Callable[[], date] = lambda: datetime.now(KST).date(),
                 mode_budget_seconds: float = 45, access_service: AccessService | None = None,
                 local_origin_hub_scan_limit: int = Settings.local_origin_hub_scan_limit,
                 local_origin_hub_limit: int = Settings.local_origin_hub_limit) -> None:
        if not 1 <= max_hubs <= 3 or not 1 <= per_type_limit <= 10 or not 1 <= total_limit <= 10:
            raise ValueError("Invalid candidate limits")
        self.providers = {TransportType.TRAIN: train, TransportType.EXPRESS_BUS: express,
                          TransportType.INTERCITY_BUS: intercity}
        self.kakao = kakao
        self.max_hubs = max_hubs
        self.per_type_limit = per_type_limit
        self.total_limit = total_limit
        self.today = today
        self.mode_budget_seconds = mode_budget_seconds
        self.access_service = access_service
        if not 1 <= local_origin_hub_scan_limit <= 30 or not 1 <= local_origin_hub_limit <= 3:
            raise ValueError("Invalid local origin limits")
        self.local_origin_hub_scan_limit = local_origin_hub_scan_limit
        self.local_origin_hub_limit = local_origin_hub_limit

    def search_return_candidates(self, trip: TripRequest, original_origin,
                                 *, earliest_departure: datetime | None = None) -> list[TransportCandidate]:
        """Destination → origin-region hubs. Never filters or mutates outbound results."""
        from services.location_service import region_name
        parts = (original_origin.address or original_origin.name or "").split()
        if len(parts) < 2:
            raise ProviderError("RETURN_REGION_UNKNOWN")
        province = region_name(parts[0])
        city = region_name(parts[1]) if province in {"경기", "경북", "경남", "전북", "전남", "충북", "충남", "강원", "제주"} else province
        # Keep the concrete origin query so TAGO does not exact-match only "서울" station.
        arrival = LocationContext(original_origin.address or original_origin.name or city, province, city)
        departure = resolve_context(trip.departure, self.kakao)
        resolver = HubResolver(self.max_hubs)
        resolver.origin_context = arrival
        access = self.access_service
        if access is not None:
            access_cache = AccessCache()
            scans: dict[TransportType, int] = {}
            ranked_origin_hubs: dict[TransportType, list] = {}

            def select_origin_hubs(hubs, train):
                kind = TransportType.TRAIN if train else TransportType.EXPRESS_BUS
                mode_key = getattr(resolver, "active_mode", kind)
                scanned = scans.get(mode_key, 0)
                from services.place_service import distance_meters
                nearby = []
                unresolved = skipped = 0
                for hub in hubs:
                    query = hub.name if (hub.name.endswith("역") if train else "터미널" in hub.name) else hub.name + ("역" if train else "버스터미널")
                    if query not in access_cache.points:
                        if scanned >= self.local_origin_hub_scan_limit:
                            skipped += 1
                            continue
                        scanned += 1
                        scans[mode_key] = scanned
                        try:
                            access_cache.points[query] = access.resolve_point(query)
                        except ProviderError:
                            access_cache.points[query] = None
                    point = access_cache.points[query]
                    if point is not None:
                        nearby.append((distance_meters(original_origin, point.y, point.x), hub.id, hub))
                    else:
                        unresolved += 1
                nearby.sort(key=lambda item: (item[0], item[1]))
                ranked = [item[2] for item in nearby]
                ranked_origin_hubs[mode_key] = ranked
                selected = ranked[:self.local_origin_hub_limit]
                logger.info("return_mode hub_catalog=%d hub_resolved=%d hub_unknown=%d hub_selected=%d",
                            len(hubs), len(nearby), unresolved, len(selected))
                return HubMatch(tuple(selected),
                                f"RETURN LOCAL_ORIGIN 거점 · 좌표 미확인 {unresolved}개 · 탐색 한도 제외 {skipped}개",
                                len(ranked) > self.local_origin_hub_limit)

            resolver.origin_selector = select_origin_hubs
            resolver.ranked_origin_hubs = ranked_origin_hubs
            # LOCAL_ORIGIN is the home-side (arrival) for return; expand that side when nearest hubs lack trains.
            resolver.expand_arrival = True
        else:
            # Without access resolution, fall back to city-level hubs (still independent of outbound).
            arrival = LocationContext(city, province, city)
            resolver.origin_context = arrival
        candidates = []
        for kind, provider in self.providers.items():
            if kind == TransportType.INTERCITY_BUS and trip.start_date != self.today():
                continue
            rows, status = self._search_mode(trip, kind, provider, departure, arrival, resolver)
            logger.info("return_mode=%s candidates=%d status=%s", kind.value, len(rows), status.status)
            candidates.extend(rows)
        earliest = earliest_departure or datetime.combine(trip.start_date, trip.departure_time, KST)
        return rank_return_candidates(candidates, trip, earliest_departure=earliest)

    def search(self, trip: TripRequest, *, origin_address: str | None = None) -> TransportResult:
        departure = (LocationContext(trip.departure) if self.access_service is not None and self.kakao is not None
                     else resolve_context(trip.departure, self.kakao))
        arrival = resolve_context(trip.destination, self.kakao)
        resolver = HubResolver(self.max_hubs)
        result = TransportResult(access_checked=self.access_service is not None)
        access_cache = AccessCache()
        if self.access_service is not None and self.kakao is not None:
            try:
                origin = (self.access_service.resolve_origin(trip.departure, origin_address) if origin_address is not None
                          else self.access_service.resolve_origin(trip.departure))
            except ProviderError as exc:
                result.origin_status = ("NEED_ADDRESS" if exc.code == "NEED_ADDRESS" else "AMBIGUOUS_ORIGIN" if exc.code == "AMBIGUOUS_ORIGIN" else
                                        "INVALID_ORIGIN" if exc.code in {"ACCESS_TIME_UNKNOWN", "INVALID_ORIGIN"} else "ORIGIN_UNAVAILABLE")
                result.user_message = ("출발지 장소명을 정확히 찾지 못했습니다. 도로명 주소 또는 지번 주소를 입력해주세요."
                                       if result.origin_status == "NEED_ADDRESS" else "출발지를 확인하지 못했습니다. 역명, 건물명 또는 주소를 조금 더 구체적으로 입력해주세요."
                                       if result.origin_status in {"INVALID_ORIGIN", "AMBIGUOUS_ORIGIN"} else
                                       "출발지 조회가 원활하지 않습니다. 잠시 후 다시 조회해주세요.")
                return result
            access_cache.points[trip.departure] = origin
            # Derive region from the selected POI, not unrelated keyword results.
            from services.location_service import region_name
            address = origin.address.split()
            if len(address) >= 2:
                province = region_name(address[0])
                city = region_name(address[1]) if province in {"경기", "경북", "경남", "전북", "전남", "충북", "충남", "강원", "제주"} else province
                departure = LocationContext(trip.departure, province, city)
            result.origin_status = "LOCAL_ORIGIN"
            resolver.origin_context = departure
            scans = {}
            ranked_origin_hubs: dict = {}

            def select_origin_hubs(hubs, train):
                mode_key = getattr(resolver, "active_mode",
                                   TransportType.TRAIN if train else TransportType.EXPRESS_BUS)
                scanned = scans.get(mode_key, 0)
                from services.place_service import distance_meters
                nearby = []
                unresolved = skipped = 0
                for hub in hubs:
                    query = hub.name if (hub.name.endswith("역") if train else "터미널" in hub.name) else hub.name + ("역" if train else "버스터미널")
                    if query not in access_cache.points:
                        if scanned >= self.local_origin_hub_scan_limit:
                            skipped += 1
                            continue
                        scanned += 1
                        scans[mode_key] = scanned
                        try:
                            access_cache.points[query] = self.access_service.resolve_point(query)
                        except ProviderError:
                            access_cache.points[query] = None
                    point = access_cache.points[query]
                    if point is not None:
                        nearby.append((distance_meters(origin, point.y, point.x), hub.id, hub))
                    else:
                        unresolved += 1
                nearby.sort(key=lambda item: (item[0], item[1]))
                ranked = [item[2] for item in nearby]
                ranked_origin_hubs[mode_key] = ranked
                selected = ranked[:self.local_origin_hub_limit]
                logger.info("mode=%s hub_catalog=%d hub_resolved=%d hub_unknown=%d hub_scan_skipped=%d hub_selected=%d",
                            mode_key.value, len(hubs), len(nearby), unresolved, skipped, len(selected))
                return HubMatch(tuple(selected),
                                f"LOCAL_ORIGIN 거리 기준 거점 탐색 · 좌표 미확인 {unresolved}개 · 탐색 한도 제외 {skipped}개",
                                len(ranked) > self.local_origin_hub_limit)

            resolver.origin_selector = select_origin_hubs
            resolver.ranked_origin_hubs = ranked_origin_hubs
        for kind, provider in self.providers.items():
            if kind == TransportType.INTERCITY_BUS and trip.start_date != self.today():
                result.by_type[kind] = []
                result.statuses.append(TransportStatus(kind, "날짜 제한",
                    "시외버스는 당일 배차만 조회합니다. 여행 당일 다시 확인하세요."))
                continue
            candidates, status = self._search_mode(trip, kind, provider, departure, arrival, resolver)
            raw_count = len(candidates)
            if self.access_service is not None:
                candidates, infeasible, unknown = self.access_service.filter_candidates(candidates, trip, access_cache)
                access_count = len(candidates)
                candidates = rank_candidates(candidates, trip)
                logger.info(
                    "mode=%s provider_raw=%d access_feasible=%d access_infeasible=%d "
                    "access_unknown=%d after_rank=%d",
                    kind.value, raw_count, access_count, infeasible, unknown, len(candidates))
                if infeasible or unknown:
                    status = TransportStatus(kind, "일부 결과" if candidates else "접근 조건 미충족",
                        status.detail + f" · 접근 검증 후 {len(candidates)}개 · 탑승 불가 {infeasible}개 제외"
                        + f" · ACCESS_TIME_UNKNOWN {unknown}개 제외", status.departure_match, status.arrival_match)
            else:
                logger.info("mode=%s provider_raw=%d access_checked=false after_rank=%d",
                            kind.value, raw_count, len(candidates))
            result.by_type[kind] = candidates[:self.per_type_limit]
            result.statuses.append(status)
        result.candidates = rank_candidates(
            [c for rows in result.by_type.values() for c in rows], trip)[:self.total_limit]
        logger.info(
            "outbound_final candidate_count=%d by_type=%s",
            len(result.candidates),
            {k.value: len(v) for k, v in result.by_type.items()})
        if any(c.access and c.access.access_leg.origin_point and c.access.access_leg.destination_point
               and c.access.access_leg.origin_point.id == c.access.access_leg.destination_point.id
               for c in result.candidates):
            result.origin_status = "DIRECT_HUB"
        return result

    def _search_mode(self, trip: TripRequest, kind: TransportType,
                     provider: TagoTrainProvider | TagoBusProvider,
                     departure: LocationContext, arrival: LocationContext,
                     resolver: HubResolver) -> tuple[list[TransportCandidate], TransportStatus]:
        started = monotonic()
        dep_match = arr_match = None
        candidates: list[TransportCandidate] = []
        failures, invalid, queried = 0, 0, 0
        limited = False
        last_error = ""
        try:
            match = resolver.train if kind == TransportType.TRAIN else resolver.bus
            resolver.active_mode = kind
            dep_match = match(departure, provider)
            arr_match = match(arrival, provider)
            if not dep_match.hubs or not arr_match.hubs:
                return [], TransportStatus(kind, "매칭 실패",
                    "출발 또는 도착 교통 거점을 확인하지 못했습니다.", dep_match, arr_match)
            limited = dep_match.limited or arr_match.limited
            origin_hubs = list(dep_match.hubs)
            used = {hub.id for hub in origin_hubs}

            def query_hubs(hubs) -> None:
                nonlocal failures, invalid, queried, limited, last_error
                for dep in hubs:
                    for arr in arr_match.hubs:
                        if dep.id == arr.id:
                            continue
                        if monotonic() - started > self.mode_budget_seconds:
                            limited = True
                            return
                        queried += 1
                        try:
                            rows = provider.search_trips(dep.id, arr.id, trip.start_date)
                        except ProviderError as exc:
                            failures += 1
                            last_error = describe_error(exc.code)
                            limited = True
                            return
                        for row in rows:
                            try:
                                candidates.append(convert_candidate(row, kind))
                            except (ValueError, ValidationError):
                                invalid += 1
                    if failures or monotonic() - started > self.mode_budget_seconds:
                        return

            query_hubs(origin_hubs)
            # Distance-nearest hubs may lack long-distance service (e.g. Yangjae → Suseo).
            # Expand to farther LOCAL_ORIGIN hubs that still have real schedules.
            ranked_hubs = list(getattr(resolver, "ranked_origin_hubs", {}).get(kind, []))
            expand_arrival = bool(getattr(resolver, "expand_arrival", False))
            if not candidates and ranked_hubs and not failures:
                if expand_arrival:
                    used_arr = {hub.id for hub in arr_match.hubs}
                    extras = [hub for hub in ranked_hubs if hub.id not in used_arr]
                    served = []
                    for hub in extras:
                        if monotonic() - started > self.mode_budget_seconds:
                            limited = True
                            break
                        before = len(candidates)
                        for dep in origin_hubs:
                            if dep.id == hub.id:
                                continue
                            if monotonic() - started > self.mode_budget_seconds:
                                limited = True
                                break
                            queried += 1
                            try:
                                rows = provider.search_trips(dep.id, hub.id, trip.start_date)
                            except ProviderError as exc:
                                failures += 1
                                last_error = describe_error(exc.code)
                                limited = True
                                break
                            for row in rows:
                                try:
                                    candidates.append(convert_candidate(row, kind))
                                except (ValueError, ValidationError):
                                    invalid += 1
                        if failures:
                            break
                        if len(candidates) > before:
                            served.append(hub)
                            used_arr.add(hub.id)
                            if len(served) >= self.local_origin_hub_limit:
                                break
                    if served:
                        arr_match = HubMatch(tuple(list(arr_match.hubs) + served),
                                             arr_match.detail + f" · 배차 있는 귀가 거점 추가 {len(served)}개", True)
                        limited = True
                        logger.info("mode=%s return_hub_expand served=%d total_arrival=%d",
                                    kind.value, len(served), len(arr_match.hubs))
                else:
                    extras = [hub for hub in ranked_hubs if hub.id not in used]
                    served = []
                    for hub in extras:
                        if monotonic() - started > self.mode_budget_seconds:
                            limited = True
                            break
                        before = len(candidates)
                        query_hubs([hub])
                        if len(candidates) > before:
                            served.append(hub)
                            used.add(hub.id)
                            if len(served) >= self.local_origin_hub_limit:
                                break
                        if failures:
                            break
                    if served:
                        origin_hubs = origin_hubs + served
                        dep_match = HubMatch(tuple(origin_hubs),
                                             dep_match.detail + f" · 배차 있는 거점 추가 {len(served)}개", True)
                        limited = True
                        logger.info("mode=%s hub_expand served=%d total_origin=%d",
                                    kind.value, len(served), len(origin_hubs))
        except ProviderError as exc:
            return [], TransportStatus(kind, "조회 실패", describe_error(exc.code), dep_match, arr_match)
        ranked = rank_candidates(candidates, trip)
        status = "일부 결과" if limited or failures or invalid else ("정상" if ranked else "후보 없음")
        if failures and not candidates:
            status = "조회 실패"
        detail = f"{queried}개 거점 조합 조회 · 조건에 맞는 후보 {len(ranked)}개"
        if invalid:
            detail += f" · 필수 데이터 오류 {invalid}개 제외"
        if limited:
            detail += " · 탐색 범위/시간 제한 또는 일부 조회 실패: 전체 최적 순위는 아닙니다."
        if last_error:
            detail += " " + last_error
        logger.info("mode=%s queried=%d valid=%d invalid=%d failed=%d latency_ms=%d",
                    kind.value, queried, len(ranked), invalid, failures, (monotonic() - started) * 1000)
        return ranked, TransportStatus(kind, status, detail, dep_match, arr_match)


def search_transport(trip: TripRequest, settings: Settings, *, origin_address: str | None = None) -> TransportResult:
    http = HttpClient(max_attempts=2)
    try:
        kakao = KakaoProvider(settings.kakao_rest_api_key, http)
        return TransportService(
            TagoTrainProvider(settings.data_go_kr_api_key, http),
            TagoBusProvider(settings.data_go_kr_api_key, http, "express"),
            TagoBusProvider(settings.data_go_kr_api_key, http, "intercity"),
            kakao, local_origin_hub_scan_limit=settings.local_origin_hub_scan_limit,
            local_origin_hub_limit=settings.local_origin_hub_limit, access_service=AccessService(kakao, KakaoTransitProvider(settings.kakao_rest_api_key, http)),
        ).search(trip, origin_address=origin_address)
    finally:
        http.close()
