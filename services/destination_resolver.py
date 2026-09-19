"""Kakao-grounded destination scope resolver — no LLM, no city hardcoding."""
from __future__ import annotations

import logging
import math
import re
from collections import Counter, OrderedDict
from typing import Any

from models.access import AccessPoint
from models.destination import AdminScope, ResolutionType, ResolvedDestination
from providers.http_client import ProviderError
from providers.kakao_provider import KakaoProvider
from services.location_service import region_name

logger = logging.getLogger("travel_ai.destination_resolver")

_METRO = frozenset({"서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종"})
_PROVINCE = frozenset({"경기", "경북", "경남", "전북", "전남", "충북", "충남", "강원", "제주"})

# Process cache shared across Place / Accommodation layers within one Cloud process.
_CACHE: OrderedDict[str, ResolvedDestination] = OrderedDict()
_CACHE_MAX = 64

FAILED_NOTICE = "여행지역을 정확히 확인하지 못했습니다. 지역명을 조금 더 구체적으로 입력해주세요."
AMBIGUOUS_NOTICE = (
    "여행지역을 정확히 확인하기 어렵습니다. "
    "예: '성수동', '서울 성수', '부산 해운대'처럼 지역명을 조금 더 구체적으로 입력해주세요."
)


def clear_destination_cache() -> None:
    _CACHE.clear()


def destination_cache_size() -> int:
    return len(_CACHE)


def normalize_destination_query(value: str) -> str:
    text = " ".join((value or "").strip().split())
    return re.sub(r"\s+", " ", text)


def parse_admin_scope(address: str) -> AdminScope | None:
    parts = [p for p in (address or "").split() if p]
    if len(parts) < 2:
        return None
    province = region_name(parts[0])
    if province in _METRO:
        district = region_name(parts[1]) if len(parts) > 1 else ""
        return AdminScope(province=province, city=province, district=district)
    if province in _PROVINCE:
        city = region_name(parts[1])
        district = region_name(parts[2]) if len(parts) > 2 else ""
        return AdminScope(province=province, city=city, district=district)
    # Fallback: treat first two tokens as province/city.
    return AdminScope(province=province, city=region_name(parts[1]),
                      district=region_name(parts[2]) if len(parts) > 2 else "")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def scope_matches_address(scope: AdminScope, address: str) -> bool:
    parsed = parse_admin_scope(address)
    if parsed is None:
        return False
    if scope.province != parsed.province:
        return False
    if scope.city != parsed.city:
        return False
    if not scope.district:
        return True
    if not parsed.district:
        return False
    return (scope.district == parsed.district
            or parsed.district.startswith(scope.district)
            or scope.district.startswith(parsed.district))


def matches_resolved_destination(
        resolved: ResolvedDestination,
        address: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
) -> bool:
    """Validate a Kakao place/lodging row against a resolved destination scope."""
    if not resolved.is_resolved:
        return False
    if resolved.resolution_type in {ResolutionType.ADMIN_REGION, ResolutionType.MULTI_ADMIN_REGION}:
        return any(scope_matches_address(scope, address) for scope in resolved.administrative_scopes)
    if resolved.resolution_type in {ResolutionType.AREA_CENTER, ResolutionType.DIRECT_PLACE}:
        if (resolved.center_latitude is not None and resolved.center_longitude is not None
                and latitude is not None and longitude is not None and resolved.radius_m > 0):
            return _haversine_m(
                resolved.center_latitude, resolved.center_longitude, latitude, longitude
            ) <= resolved.radius_m
        # Soft fallback: accept if address hits any observed admin scopes.
        if resolved.administrative_scopes:
            return any(scope_matches_address(scope, address) for scope in resolved.administrative_scopes)
        return False
    return False


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def _store(key: str, value: ResolvedDestination) -> ResolvedDestination:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return value


def _coords_from_doc(doc: dict[str, Any]) -> tuple[float, float] | None:
    for src in (doc, doc.get("address") if isinstance(doc.get("address"), dict) else None):
        if not src:
            continue
        try:
            return float(src["y"]), float(src["x"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _relation_to_query(scope: AdminScope, address_name: str, normalized: str) -> str:
    """Return city|district|dong|'' describing how an address relates to the query token."""
    if normalized == scope.city or scope.city.startswith(normalized) or normalized.startswith(scope.city):
        return "city"
    if scope.district and (scope.district.startswith(normalized) or normalized in scope.district):
        return "district"
    for token in address_name.split():
        bare = region_name(token)
        if bare == normalized or bare.startswith(normalized) or normalized in bare:
            return "dong"
    return ""


def _resolve_from_addresses(
        kakao: KakaoProvider,
        query: str,
        display: str,
        normalized: str,
        area_radius_m: int,
) -> ResolvedDestination | None:
    """STEP 2 — Kakao Address API (official / multi-admin / dong living area)."""
    try:
        docs = kakao.search_addresses(display)
    except ProviderError:
        return None
    if not docs:
        return None

    samples: list[tuple[AdminScope, float, float, str, str]] = []
    for doc in docs:
        name = str(doc.get("address_name") or "").strip()
        if not name:
            continue
        scope = parse_admin_scope(name)
        if scope is None and region_name(name) in _METRO:
            metro = region_name(name)
            scope = AdminScope(province=metro, city=metro, district="")
        if scope is None:
            continue
        coords = _coords_from_doc(doc)
        if coords is None:
            continue
        relation = _relation_to_query(scope, name, normalized)
        if not relation:
            continue
        samples.append((scope, coords[0], coords[1], name, relation))

    if not samples:
        return None

    source = "KAKAO_ADDRESS"
    # Prefer city/district admin matches over same-name dong in other cities (구미시 vs 구미동).
    if any(s[4] == "city" for s in samples):
        samples = [s for s in samples if s[4] == "city"]
    elif any(s[4] == "district" for s in samples):
        samples = [s for s in samples if s[4] == "district"]

    city_key = Counter((s[0].province, s[0].city) for s in samples)
    if len(city_key) >= 2:
        # Distinct cities for the same token → ambiguous at address layer.
        return ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            status="AMBIGUOUS", notice=AMBIGUOUS_NOTICE,
            resolution_type=ResolutionType.AMBIGUOUS, source=source)

    (province, city), _ = city_key.most_common(1)[0]
    city_samples = [s for s in samples if s[0].province == province and s[0].city == city]
    center_lat, center_lon = _centroid([(s[1], s[2]) for s in city_samples])
    relations = {s[4] for s in city_samples}

    if "city" in relations:
        return ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            resolution_type=ResolutionType.ADMIN_REGION,
            center_latitude=center_lat, center_longitude=center_lon,
            administrative_scopes=(AdminScope(province=province, city=city, district=""),),
            radius_m=0, status="RESOLVED", source=source)

    district_counts = Counter(s[0].district for s in city_samples if s[0].district)
    matching = sorted(
        d for d in district_counts
        if d and (d.startswith(normalized) or normalized in d or d == normalized))
    if len(matching) >= 2:
        scopes = tuple(AdminScope(province=province, city=city, district=d) for d in matching)
        pts = [(s[1], s[2]) for s in city_samples if s[0].district in matching]
        clat, clon = _centroid(pts or [(center_lat, center_lon)])
        return ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            resolution_type=ResolutionType.MULTI_ADMIN_REGION,
            center_latitude=clat, center_longitude=clon,
            administrative_scopes=scopes, radius_m=0, status="RESOLVED", source=source)

    if len(matching) == 1 or (province in _METRO and normalized in district_counts):
        district = matching[0] if matching else normalized
        return ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            resolution_type=ResolutionType.ADMIN_REGION,
            center_latitude=center_lat, center_longitude=center_lon,
            administrative_scopes=(AdminScope(province=province, city=city, district=district),),
            radius_m=0, status="RESOLVED", source=source)

    # Dong / neighborhood address hits (성수동) → area center around address coords.
    observed = tuple(
        AdminScope(province=province, city=city, district=d)
        for d, _ in district_counts.most_common(3) if d)
    if not observed:
        observed = (AdminScope(province=province, city=city, district=""),)
    return ResolvedDestination(
        original_query=query, display_name=display, normalized_query=normalized,
        resolution_type=ResolutionType.AREA_CENTER,
        center_latitude=center_lat, center_longitude=center_lon,
        administrative_scopes=observed, radius_m=area_radius_m,
        status="RESOLVED", source=source)


def resolve_destination(
        kakao: KakaoProvider,
        query: str,
        *,
        hub: AccessPoint | None = None,
        area_radius_m: int = 3500,
        sample_size: int = 15,
        hub_assist_meters: int = 45000,
) -> ResolvedDestination:
    """Resolve a user destination string using Kakao Local results only."""
    display = normalize_destination_query(query)
    if not display:
        return ResolvedDestination(
            original_query=query or "-", display_name="-", normalized_query="-",
            status="FAILED", notice=FAILED_NOTICE, resolution_type=ResolutionType.FAILED)
    normalized = region_name(display.split()[-1])
    cache_key = f"{normalized}|{hub.id if hub else ''}|{area_radius_m}"
    cached = _CACHE.get(cache_key)
    if cached is not None:
        _CACHE.move_to_end(cache_key)
        logger.info("destination_resolve outcome=cache_hit query=%s type=%s",
                    display, cached.resolution_type.value)
        return cached

    # STEP 2 — Address / Region resolution first (마산 → 합포+회원, 구미 → 구미시).
    addressed = _resolve_from_addresses(kakao, query, display, normalized, area_radius_m)
    if addressed is not None:
        logger.info("destination_resolve outcome=%s query=%s source=%s scopes=%s",
                    addressed.resolution_type.value, display, addressed.source,
                    [s.district or s.city for s in addressed.administrative_scopes])
        return _store(cache_key, addressed)

    # STEP 3+ — Keyword / place cluster resolution (홍대, 생활권, place-like).
    rows: list[dict[str, Any]] = []
    source = "KAKAO_KEYWORD"
    try:
        rows = kakao.search_places(display, size=min(15, max(5, sample_size)))
    except ProviderError:
        logger.warning("destination_resolve outcome=search_failed query=%s", display)
        return _store(cache_key, ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            status="FAILED", notice=FAILED_NOTICE, resolution_type=ResolutionType.FAILED,
            source=source))

    samples: list[tuple[AdminScope, float, float, str]] = []
    for row in rows:
        address = row.get("address_name") or row.get("road_address_name") or ""
        if not isinstance(address, str) or not address.strip():
            continue
        scope = parse_admin_scope(address)
        if scope is None:
            continue
        try:
            lon = float(row["x"])
            lat = float(row["y"])
        except (KeyError, TypeError, ValueError):
            continue
        samples.append((scope, lat, lon, address))

    if not samples:
        return _store(cache_key, ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            status="FAILED", notice=FAILED_NOTICE, resolution_type=ResolutionType.FAILED,
            source=source))

    # Arrival hub assists outlier removal only — does not alone define the scope.
    if hub is not None:
        near = [s for s in samples
                if _haversine_m(hub.y, hub.x, s[1], s[2]) <= hub_assist_meters]
        if len(near) >= max(3, len(samples) // 3):
            samples = near
            source = "KAKAO_KEYWORD+ARRIVAL_HUB_ASSISTED"

    city_key = Counter((s[0].province, s[0].city) for s in samples)
    (province, city), city_hits = city_key.most_common(1)[0]
    if len(city_key) >= 3 and city_hits < max(3, int(len(samples) * 0.35)):
        return _store(cache_key, ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            status="AMBIGUOUS", notice=AMBIGUOUS_NOTICE,
            resolution_type=ResolutionType.AMBIGUOUS, source=source))

    city_samples = [s for s in samples if s[0].province == province and s[0].city == city]
    center_lat, center_lon = _centroid([(s[1], s[2]) for s in city_samples])

    # A. Official / plain city match (구미 → 구미시).
    if normalized == city or city.startswith(normalized) or normalized.startswith(city):
        resolved = ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            resolution_type=ResolutionType.ADMIN_REGION,
            center_latitude=center_lat, center_longitude=center_lon,
            administrative_scopes=(AdminScope(province=province, city=city, district=""),),
            radius_m=0, status="RESOLVED", source=source)
        logger.info("destination_resolve outcome=admin query=%s city=%s/%s", display, province, city)
        return _store(cache_key, resolved)

    # Metro district as destination (해운대 → 부산/해운대).
    district_counts = Counter(s[0].district for s in city_samples if s[0].district)
    if province in _METRO and normalized in district_counts:
        resolved = ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            resolution_type=ResolutionType.ADMIN_REGION,
            center_latitude=center_lat, center_longitude=center_lon,
            administrative_scopes=(AdminScope(province=province, city=city, district=normalized),),
            radius_m=0, status="RESOLVED", source=source)
        logger.info("destination_resolve outcome=metro_district query=%s %s/%s",
                    display, province, normalized)
        return _store(cache_key, resolved)

    # B. Multi-admin: districts whose names share the query token (마산→마산합포/마산회원).
    matching_districts = sorted(
        d for d, _ in district_counts.most_common()
        if d and (d.startswith(normalized) or normalized in d))
    # Accept even a single keyword district hit; address layer usually supplies siblings.
    if len(matching_districts) >= 2 or (
            len(matching_districts) == 1 and district_counts[matching_districts[0]] >= 2):
        scopes = tuple(AdminScope(province=province, city=city, district=d)
                       for d in matching_districts)
        matched_points = [(s[1], s[2]) for s in city_samples if s[0].district in matching_districts]
        clat, clon = _centroid(matched_points or [(center_lat, center_lon)])
        rtype = (ResolutionType.MULTI_ADMIN_REGION if len(matching_districts) >= 2
                 else ResolutionType.ADMIN_REGION)
        resolved = ResolvedDestination(
            original_query=query, display_name=display, normalized_query=normalized,
            resolution_type=rtype,
            center_latitude=clat, center_longitude=clon,
            administrative_scopes=scopes, radius_m=0, status="RESOLVED", source=source)
        logger.info("destination_resolve outcome=%s query=%s scopes=%s",
                    rtype.value, display, [s.district for s in scopes])
        return _store(cache_key, resolved)

    # C/D. Area / place-centered living zone (홍대, 성수) — radius, not address substring.
    observed = tuple(
        AdminScope(province=province, city=city, district=d)
        for d, _ in district_counts.most_common(3) if d)
    if not observed:
        observed = (AdminScope(province=province, city=city, district=""),)
    place_like = any(
        display in str(row.get("place_name") or "") or display in str(row.get("category_name") or "")
        for row in rows[:5])
    rtype = ResolutionType.DIRECT_PLACE if place_like and display.endswith(
        ("역", "터미널", "해수욕장", "공원")) else ResolutionType.AREA_CENTER
    resolved = ResolvedDestination(
        original_query=query, display_name=display, normalized_query=normalized,
        resolution_type=rtype,
        center_latitude=center_lat, center_longitude=center_lon,
        administrative_scopes=observed,
        radius_m=area_radius_m, status="RESOLVED", source=source)
    logger.info("destination_resolve outcome=%s query=%s radius=%d",
                rtype.value, display, area_radius_m)
    return _store(cache_key, resolved)
