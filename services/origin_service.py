"""Conservative user-origin resolution; transport-hub identity rules stay separate."""
import hashlib
import logging
import re
import unicodedata
from math import asin, cos, radians, sin, sqrt

from pydantic import ValidationError

from models.access import AccessPoint
from providers.http_client import ProviderError
from services.location_service import region_name

logger = logging.getLogger("travel_ai.origin")
STATION_CLUSTER_METERS = 300
MIN_SCORE = 80
SCORE_MARGIN = 15
GENERIC_ORIGINS = {"집", "우리집", "회사", "현재위치", "여기", "내집", "직장"}


def normalized(value: str) -> str:
    value = re.sub(r"\([^)]*\)", "", unicodedata.normalize("NFKC", value)).casefold()
    return re.sub(r"[^\w가-힣]", "", value)


def station_stem(value: str) -> str:
    # Keep the 역 suffix; strip a line suffix only in station-category candidates.
    value = re.sub(r"\([^)]*\)", "", value).strip()
    match = re.fullmatch(r"(.+?역)(?:\s*(?:\d+호선|[\w-]+선|GTX-[A-Z]))?", value, re.I)
    return normalized(match.group(1)) if match else normalized(value)


def region_tokens(address: str) -> tuple[str, ...]:
    return tuple(region_name(part) for part in address.split()[:2])


def separation(a: AccessPoint, b: AccessPoint) -> float:
    angle = sin(radians(b.y-a.y)/2)**2 + cos(radians(a.y))*cos(radians(b.y))*sin(radians(b.x-a.x)/2)**2
    return 6371000 * 2 * asin(sqrt(min(1, max(0, angle))))


class OriginResolver:
    def __init__(self, kakao):
        self.kakao = kakao

    def resolve(self, query: str) -> AccessPoint:
        query = " ".join(query.split())
        if not query or normalized(query) in GENERIC_ORIGINS:
            raise ProviderError("INVALID_ORIGIN")
        error = None
        try:
            rows = list(self.kakao.search_places(query, size=15))
        except ProviderError as exc:
            rows, error = [], exc
        # At most two extra whitespace variants, only when original evidence is insufficient.
        variants = list(dict.fromkeys((query, re.sub(r"(?<=\D)(\d+\s*차)", r" \1", query),
                                       re.sub(r"\s+", "", query))))
        def relevant(row):
            try:
                point = AccessPoint(id=row["id"], name=row["place_name"], x=row["x"], y=row["y"],
                                    address=row.get("address_name") or "", category=row.get("category_name") or "")
                return self.score(query, point) >= MIN_SCORE
            except (KeyError, TypeError, ValidationError):
                return False
        if error is None and not any(relevant(row) for row in rows):
            for variant in variants[1:3]:
                try:
                    rows += self.kakao.search_places(variant, size=15)
                except ProviderError as exc:
                    error = exc
                    break
                if any(relevant(row) for row in rows):
                    break
        candidates = {}
        for row in rows:
            try:
                point = AccessPoint(id=row["id"], name=row["place_name"], x=row["x"], y=row["y"],
                    address=row.get("address_name") or row.get("road_address_name") or "",
                    category=row.get("category_name") or "", original_query=query, source="KAKAO_KEYWORD")
            except (KeyError, TypeError, ValidationError):
                continue
            score = self.score(query, point)
            if score >= MIN_SCORE:
                candidates[point.id] = (score, point)
        ranked = sorted(candidates.values(), key=lambda pair: (-pair[0], pair[1].id))
        if ranked:
            best = ranked[0]
            contenders = [p for score, p in ranked if best[0] - score < SCORE_MARGIN]
            if len(contenders) == 1:
                return best[1]
            if self.same_station(contenders):
                # Return one real API POI, not an invented centroid or merged public ID.
                return best[1]
            logger.info("origin outcome=ambiguous candidates=%d", len(contenders))
            if not re.search(r"(?:로|길|동|읍|면|리)\s*\d", query):
                raise ProviderError("AMBIGUOUS_ORIGIN")
        if error is None and not re.search(r"(?:로|길|동|읍|면|리)\s*\d", query):
            logger.info("origin outcome=need_address")
            raise ProviderError("NEED_ADDRESS")
        return self.resolve_address(query, original_query=query, prior_error=error)

    def resolve_address(self, query: str, *, original_query: str = "", prior_error=None) -> AccessPoint:
        if not query.strip() or normalized(query) in GENERIC_ORIGINS:
            raise ProviderError("INVALID_ORIGIN")
        try:
            addresses = self.kakao.search_addresses(query)
        except ProviderError as exc:
            raise prior_error or exc
        found = {}
        for row in addresses:
            # REGION/ROAD results are centroids, not a sufficiently specific origin.
            if row.get("address_type") not in {"ROAD_ADDR", "REGION_ADDR"}:
                continue
            address = row.get("address_name")
            if not isinstance(address, str) or not address.strip():
                continue
            try:
                point = AccessPoint(id="pending", name=address, address=address, x=row["x"], y=row["y"],
                                    original_query=original_query or query, source="KAKAO_ADDRESS")
            except (KeyError, TypeError, ValidationError):
                continue
            # Synthetic internal identity, never presented as a Kakao POI ID.
            identity = hashlib.sha256(f"{point.x:.7f},{point.y:.7f}:{normalized(address)}".encode()).hexdigest()
            found[identity] = point.model_copy(update={"id": "address:" + identity})
        if len(found) == 1:
            logger.info("origin outcome=resolved source=address")
            return next(iter(found.values()))
        if found:
            raise ProviderError("AMBIGUOUS_ORIGIN")
        if prior_error:
            raise prior_error
        raise ProviderError("INVALID_ORIGIN")

    @staticmethod
    def score(query: str, point: AccessPoint) -> int:
        requested, name = normalized(query), normalized(point.name)
        if requested == name:
            return 120
        if requested == normalized(point.address):
            return 110
        if query.strip().endswith("역") and station_stem(query) == station_stem(point.name) and any(
                word in point.category for word in ("지하철", "기차역")):
            return 100
        if "터미널" in requested and "버스터미널" in point.category:
            key = lambda value: re.sub(r"고속|시외|종합|버스|터미널", "", value)
            if key(requested) and key(requested) == key(name):
                return 100
        # Qualified building query: require the name and every remaining location token.
        parts = [region_name(p) for p in query.split()]
        address = [region_name(p) for p in point.address.split()]
        name_parts = station_stem(point.name) if any(t in point.category for t in ("지하철", "기차역")) else normalized(point.name)
        if len(parts) > 1 and any(normalized(p) == name_parts for p in parts):
            others = [p for p in parts if normalized(p) != name_parts]
            if others and all(p in address for p in others):
                return 100
        return 0

    @staticmethod
    def same_station(points: list[AccessPoint]) -> bool:
        if not all(any(word in p.category for word in ("지하철", "기차역")) for p in points):
            return False
        if len({station_stem(p.name) for p in points}) != 1:
            return False
        regions = [region_tokens(p.address) for p in points]
        if not all(len(r) == 2 for r in regions) or len(set(regions)) != 1:
            return False
        return all(separation(a, b) <= STATION_CLUSTER_METERS for a in points for b in points)
