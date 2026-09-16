"""Build grounded place candidates, with optional validated AI reason selection."""
import logging
from math import atan2, cos, radians, sin, sqrt
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import Settings
from models.access import AccessPoint
from models.place import CompanionStatus, PlaceCandidate, PlaceResult
from models.transport import TransportCandidate, TransportType
from models.trip_request import Preference, TripRequest
from providers.groq_provider import GroqProvider
from providers.http_client import HttpClient, ProviderError
from providers.kakao_provider import KakaoProvider
from providers.kakao_transit_provider import KakaoTransitProvider
from services.access_service import AccessService

logger = logging.getLogger("travel_ai.places")

# Query matches indicate search relevance, never verified business capabilities.
SEARCH_TERMS = {
    Preference.FOOD: "음식점", Preference.SIGHTSEEING: "관광명소", Preference.SHOPPING: "쇼핑",
    Preference.EXPERIENCE: "문화 체험", Preference.REST: "카페", Preference.NIGHT_VIEW: "야경",
    Preference.DRINK: "주점", Preference.DATE: "데이트", Preference.FAMILY: "가족 나들이",
    Preference.SOLO: "전시관",
}


PREFERENCE_SEARCH_MAPPING = {p: (term,) for p, term in SEARCH_TERMS.items()}
PREFERENCE_SEARCH_MAPPING[Preference.FOOD] = ("음식점", "맛집", "한식")
PREFERENCE_SEARCH_MAPPING[Preference.SIGHTSEEING] = (
    "관광", "관광명소", "문화유적", "테마거리", "공원", "시장", "박물관")
CATEGORY_TERMS = {
    Preference.SIGHTSEEING: ("관광", "명소", "문화유적", "테마거리", "공원", "시장", "박물관"),
    Preference.FOOD: ("음식점",), Preference.REST: ("카페", "공원"),
    Preference.SHOPPING: ("쇼핑", "시장", "백화점"), Preference.EXPERIENCE: ("체험", "문화"),
    Preference.NIGHT_VIEW: ("전망", "야경"), Preference.DRINK: ("술집", "주점"),
    Preference.DATE: ("공원", "카페"), Preference.FAMILY: ("공원", "박물관"),
    Preference.SOLO: ("전시", "미술관"),
}


MEAL_CATEGORIES = ("한식", "중식", "일식", "양식", "분식", "육류", "해물", "국밥", "면류", "뷔페")
CAFE_CATEGORIES = ("카페", "커피전문점")


def accepts_preference(category: str, preference: Preference) -> bool:
    return not (preference == Preference.FOOD and any(term in category for term in CAFE_CATEGORIES))


def place_reason(candidate: PlaceCandidate) -> str:
    parts = [candidate.category + " 카테고리의 후보입니다."] if candidate.category else []
    if candidate.role == "NEARBY_PLACE" and candidate.distance_meters is not None:
        parts.append(f"선택 기준점에서 직선거리 약 {candidate.distance_meters:.0f}m입니다.")
    if len(candidate.matched_queries) > 1:
        parts.append(f"서로 다른 관련 검색 {len(candidate.matched_queries)}개에서 확인했습니다.")
    return " ".join(parts)


def destination_tokens(value: str) -> tuple[str, ...]:
    from services.location_service import region_name
    return tuple(region_name(token) for token in value.split())


def belongs_to_destination(destination: str, address: str) -> bool:
    # Only administrative address components, never substring matches in business names.
    wanted = destination_tokens(destination)
    actual = destination_tokens(" ".join(address.split()[:3]))
    return bool(wanted) and all(token in actual for token in wanted)


def discovery_score(candidate: PlaceCandidate) -> int:
    category_matches = sum(any(term in candidate.category for term in CATEGORY_TERMS[p])
                           for p in candidate.matched_preferences)
    completeness = sum(bool(value) for value in (candidate.category, candidate.address,
                                                candidate.phone, candidate.place_url))
    meal_match = Preference.FOOD in candidate.matched_preferences and any(term in candidate.category for term in MEAL_CATEGORIES)
    detail = min(3, max(0, len(candidate.category.split(">")) - 1))
    query_category = sum(any(term in candidate.category for term in query.split()) for query in candidate.matched_queries)
    return min(100, 15 + 10 * min(len(candidate.matched_preferences), 2) + 15 * min(category_matches, 2)
               + 8 * int(meal_match) + 2 * detail + 4 * min(len(candidate.matched_queries), 4)
               + 3 * min(query_category, 3) + 2 * completeness
               + (5 if candidate.role == "MAIN_DESTINATION" else 0))


def activity_radius_meters(value: str, extended: int = 2000) -> int:
    if value == "500m 이상":
        if not 501 <= extended <= 20000:
            raise ValueError("Extended radius must be 501..20000m")
        return extended
    if value not in {"100m", "200m", "300m", "400m", "500m"}:
        raise ValueError("Invalid radius")
    return int(value[:-1])


def distance_meters(anchor: AccessPoint, latitude: float, longitude: float) -> float:
    lat1, lat2 = radians(anchor.y), radians(latitude)
    angle = sin((lat2-lat1)/2)**2 + cos(lat1)*cos(lat2)*sin(radians(longitude-anchor.x)/2)**2
    return 6371000 * 2 * atan2(sqrt(max(0, min(1, angle))), sqrt(max(0, 1-angle)))


def convert_place(row: dict[str, Any], anchor: AccessPoint, preference: Preference) -> PlaceCandidate:
    # Validate finite coordinates first; recompute straight-line distance to the exact anchor.
    point = AccessPoint(id=row.get("id"), name=row.get("place_name"), x=row.get("x"), y=row.get("y"))
    url = row.get("place_url") or None
    if not isinstance(url, str):
        url = None
    if url and (urlparse(url).scheme not in {"https", "http"} or urlparse(url).hostname not in {"place.map.kakao.com", "map.kakao.com"}):
        url = None
    return PlaceCandidate(
        place_id=point.id, place_name=point.name, latitude=point.y, longitude=point.x,
        category=row.get("category_name") or "", address=row.get("road_address_name") or row.get("address_name") or "",
        distance_meters=distance_meters(anchor, point.y, point.x), phone=row.get("phone") or None,
        place_url=url, matched_preferences=(preference,), preference_score=100, raw_data=row.copy())


def rank_places(candidates: list[PlaceCandidate], trip: TripRequest) -> list[PlaceCandidate]:
    def completeness(c: PlaceCandidate) -> int:
        return sum(bool(v) for v in (c.category, c.address, c.phone, c.place_url))

    def conflicts(c: PlaceCandidate) -> int:
        return int(trip.has_pet and c.pet_status == CompanionStatus.NOT_SUPPORTED) + int(
            trip.has_child and c.child_status == CompanionStatus.NOT_SUPPORTED)
    def key(c: PlaceCandidate):
        if c.role == "MAIN_DESTINATION":
            return (-c.preference_score, -completeness(c), conflicts(c), c.place_id)
        distance = c.distance_meters if c.distance_meters is not None else float("inf")
        # Separate, bounded proximity contribution; never overwrite preference_score.
        proximity = 20 / (1 + distance / 200)
        return (-(c.preference_score + proximity), distance, conflicts(c), c.place_id)
    return sorted(candidates, key=key)



class ReasonChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_id: str
    preference: Preference
    reason_code: Literal["PREFERENCE_MATCH", "NEAR_ANCHOR", "MULTIPLE_PREFERENCES"]


class PlaceReasons(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choices: list[ReasonChoice] = Field(max_length=10)


def add_ai_reasons(candidates: list[PlaceCandidate], groq: GroqProvider) -> tuple[list[PlaceCandidate], str]:
    top = candidates[:10]
    if not top:
        return candidates, "후보 없음"
    payload = {"candidates": [{"place_id": c.place_id, "category": c.category,
                                "matched_preferences": [p.value for p in c.matched_preferences],
                                "distance_meters": round(c.distance_meters) if c.distance_meters is not None else None} for c in top]}
    try:
        response = groq.generate_structured(
            "For each useful supplied candidate select one matched preference and an evidence-based reason code. "
            "NEAR_ANCHOR is allowed only for distance <= 200m. MULTIPLE_PREFERENCES requires >= 2 matched preferences. "
            "Return only choices with place_id, preference and reason_code. Do not infer pet/child/allergy safety, "
            "opening hours, quality, prices or new places. Do not change the ranking.", payload, PlaceReasons)
        allowed = {c.place_id: c for c in top}
        seen = set()
        reasons = {}
        for choice in response.choices:
            c = allowed.get(choice.place_id)
            if c is None or choice.place_id in seen or choice.preference not in c.matched_preferences:
                raise ValueError("Ungrounded AI selection")
            if choice.reason_code == "NEAR_ANCHOR" and (c.distance_meters is None or c.distance_meters > 200):
                raise ValueError("Ungrounded proximity")
            if choice.reason_code == "MULTIPLE_PREFERENCES" and len(c.matched_preferences) < 2:
                raise ValueError("Ungrounded preference count")
            seen.add(choice.place_id)
            suffix = {"PREFERENCE_MATCH": "관련 검색에서 찾은 후보입니다.",
                      "NEAR_ANCHOR": "관련 후보로, 기준점에서 200m 이내입니다.",
                      "MULTIPLE_PREFERENCES": "등 여러 선택 성향의 검색에서 찾은 후보입니다."}[choice.reason_code]
            if choice.reason_code == "PREFERENCE_MATCH":
                if any(term in c.category for term in CATEGORY_TERMS[choice.preference]):
                    reasons[choice.place_id] = f"{c.category} 카테고리로, {choice.preference.value} 성향과 관련됩니다."
            else:
                reasons[choice.place_id] = place_reason(c) or f"{choice.preference.value} {suffix}"
        return [c.model_copy(update={"ai_reason": reasons.get(c.place_id)}) for c in candidates], "검증 완료"
    except (ProviderError, ValueError, ValidationError):
        logger.warning("place_ai outcome=discarded")
        return candidates, "AI 응답 미사용 · 기본 정렬 유지"


class PlaceService:
    def __init__(self, kakao: KakaoProvider, *, resolve_point: Callable[[str], AccessPoint], groq: GroqProvider | None = None,
                 extended_radius: int = 2000, limit: int = 15, pages_per_query: int = 2) -> None:
        if not 1 <= limit <= 30 or not 1 <= pages_per_query <= 3:
            raise ValueError("Invalid place search limits")
        self.kakao, self.resolve_point, self.groq = kakao, resolve_point, groq
        self.extended_radius, self.limit, self.pages_per_query = extended_radius, limit, pages_per_query

    def search_main(self, trip: TripRequest, selected_transport: TransportCandidate) -> PlaceResult:
        result = PlaceResult(destination_scope=trip.destination)
        hub = selected_transport.arrival_place
        hub += ("역" if selected_transport.transport_type == TransportType.TRAIN else "버스터미널") if not (
            hub.endswith("역") or "터미널" in hub) else ""
        try:
            result.anchor = self.resolve_point(hub)
            result.anchor_candidates = (result.anchor,)
        except ProviderError:
            result.notices.append("도착 거점 좌표 미확인: 메인 장소 검색은 계속합니다.")
        found = {}
        failed = invalid = outside = 0
        limited = False
        for preference in dict.fromkeys(trip.preferences):
            for term in PREFERENCE_SEARCH_MAPPING[preference]:
                query = f"{trip.destination} {term}"
                for page in range(1, self.pages_per_query + 1):
                    try:
                        rows = self.kakao.search_places(query, page=page, size=15)
                    except ProviderError:
                        failed += 1
                        break
                    for row in rows:
                        address = row.get("address_name") or row.get("road_address_name") or ""
                        if not isinstance(address, str) or not belongs_to_destination(trip.destination, address):
                            outside += 1
                            continue
                        try:
                            point = result.anchor or AccessPoint(id=row.get("id"), name=row.get("place_name"),
                                                                x=row.get("x"), y=row.get("y"))
                            candidate = convert_place(row, point, preference)
                        except (ValueError, TypeError, ValidationError):
                            invalid += 1
                            continue
                        if not accepts_preference(candidate.category, preference):
                            continue
                        old = found.get(candidate.place_id)
                        matches = tuple(dict.fromkeys((old.matched_preferences if old else ()) + (preference,)))
                        queries = tuple(dict.fromkeys((old.matched_queries if old else ()) + (query,)))
                        candidate = (old or candidate).model_copy(update={"role": "MAIN_DESTINATION",
                            "matched_preferences": matches, "matched_queries": queries,
                            "distance_meters": candidate.distance_meters if result.anchor else None})
                        found[candidate.place_id] = candidate.model_copy(update={"preference_score": discovery_score(candidate)})
                    if len(rows) < 15:
                        break
                    if page == self.pages_per_query:
                        limited = True
        result.candidates = rank_places(list(found.values()), trip)[:self.limit]
        result.status = ("일부 결과" if failed or invalid or limited else "정상") if result.candidates else ("조회 실패" if failed else "후보 없음")
        if failed:
            result.notices.append("일부 검색에 실패했습니다. 확보한 후보는 유지했습니다.")
        if limited:
            result.notices.append("검색 페이지 상한 내 후보입니다. 지역 전체 장소를 빠짐없이 조회한 결과는 아닙니다.")
        if not result.candidates and outside:
            result.notices.append("주소로 여행지역을 확인할 수 있는 후보가 없습니다. 시·군·구 이름으로 입력해주세요.")
        if self.groq and result.candidates and result.anchor:
            result.candidates, result.ai_status = add_ai_reasons(result.candidates, self.groq)
        logger.info("main_places count=%d invalid=%d outside=%d failed=%d", len(result.candidates), invalid, outside, failed)
        return result

    def search(self, trip: TripRequest, selected_transport: TransportCandidate,
               anchor_id: str | None = None) -> PlaceResult:
        radius = activity_radius_meters(trip.activity_radius, self.extended_radius)
        result = PlaceResult(radius_meters=radius)
        hub = selected_transport.arrival_place
        if selected_transport.transport_type == TransportType.TRAIN:
            hub = hub if hub.endswith("역") else hub + "역"
        elif "터미널" not in hub:
            hub += "버스터미널"
        anchors = []
        for query in dict.fromkeys((hub, trip.destination)):
            try:
                point = self.resolve_point(query)
            except ProviderError:
                if query == hub:
                    result.notices.append("도착 거점의 정확한 좌표를 확인하지 못했습니다.")
                continue
            if point.id not in {a.id for a in anchors}:
                anchors.append(point)
        result.anchor_candidates = tuple(anchors)
        # Default never silently substitutes a distant destination for an unresolved hub.
        if not anchors or (result.notices and anchor_id is None):
            result.status = "기준점 확인 필요"
            return result
        anchor = next((a for a in anchors if a.id == anchor_id), None) if anchor_id else anchors[0]
        if anchor is None:
            result.status = "기준점 확인 필요"
            return result
        result.anchor = anchor
        found: dict[str, PlaceCandidate] = {}
        failed = invalid = outside = 0
        limited = False
        for preference in dict.fromkeys(trip.preferences):
            for term in (PREFERENCE_SEARCH_MAPPING[preference] if preference == Preference.FOOD else (SEARCH_TERMS[preference],)):
                for page in range(1, self.pages_per_query + 1):
                    try:
                        rows = self.kakao.search_places(term, x=anchor.x, y=anchor.y,
                                                        radius=radius, page=page, size=15)
                    except ProviderError:
                        failed += 1
                        break
                    for row in rows:
                        try:
                            candidate = convert_place(row, anchor, preference)
                        except (ValueError, TypeError, ValidationError):
                            invalid += 1
                            continue
                        if candidate.place_id == anchor.id:
                            continue
                        if candidate.distance_meters > radius:
                            outside += 1
                            continue
                        if not accepts_preference(candidate.category, preference):
                            continue
                        old = found.get(candidate.place_id)
                        matches = tuple(dict.fromkeys((old.matched_preferences if old else ()) + (preference,)))
                        queries = tuple(dict.fromkeys((old.matched_queries if old else ()) + (term,)))
                        candidate = (old or candidate).model_copy(update={"matched_preferences": matches, "matched_queries": queries})
                        found[candidate.place_id] = candidate.model_copy(update={"preference_score": discovery_score(candidate)})
                    if len(rows) < 15:
                        break
                    if page == self.pages_per_query:
                        limited = True
        result.candidates = rank_places(list(found.values()), trip)[:self.limit]
        result.status = ("일부 결과" if failed or invalid or limited else "정상") if result.candidates else (
            "조회 실패" if failed else "후보 없음")
        if failed:
            result.notices.append(f"검색 {failed}건 실패 · 확보한 후보는 유지했습니다.")
        if invalid or outside:
            result.notices.append(f"필수 데이터 오류 {invalid}건, 반경 초과 {outside}건 제외")
        if limited:
            result.notices.append("검색별 페이지 상한에 도달했습니다. 전체 장소를 비교한 순위는 아닙니다.")
        if trip.has_pet or trip.has_child or trip.allergies:
            result.notices.append("반려동물·아이 이용 가능 여부와 알레르기 안전성은 확인되지 않았습니다. 방문 전 업체에 확인하세요.")
        if self.groq and result.candidates and result.anchor:
            result.candidates, result.ai_status = add_ai_reasons(result.candidates, self.groq)
        logger.info("place_search candidates=%d invalid=%d outside=%d failed=%d", len(result.candidates), invalid, outside, failed)
        return result


def search_places_for_trip(trip: TripRequest, selected_transport: TransportCandidate,
                           settings: Settings, anchor_id: str | None = None, *,
                           nearby: bool = False, point: AccessPoint | None = None) -> PlaceResult:
    http = HttpClient(max_attempts=2)
    try:
        kakao = KakaoProvider(settings.kakao_rest_api_key, http)
        resolver = AccessService(kakao, KakaoTransitProvider(settings.kakao_rest_api_key, http))
        groq = GroqProvider(settings.groq_api_key, http, settings.groq_model) if settings.groq_api_key else None
        service = PlaceService(kakao, resolve_point=(lambda query: point) if point else resolver.resolve_point,
                               groq=groq, extended_radius=settings.extended_activity_radius_meters)
        return service.search(trip, selected_transport, anchor_id) if nearby else service.search_main(trip, selected_transport)
    finally:
        http.close()
