"""Schedule supporting candidates — independent from MAIN preference recommendation."""
from __future__ import annotations

import logging

from models.place import PlaceCandidate
from models.trip_request import Preference, TripRequest
from providers.http_client import ProviderError
from services.itinerary_suitability import is_micro_landmark
from services.place_service import belongs_to_destination, convert_place, discovery_score

logger = logging.getLogger("travel_ai.schedule_support")

# Destination-scoped enrichment queries (Trip.destination is prefixed by the caller).
SUPPORT_SEARCH_TERMS: tuple[tuple[str, Preference], ...] = (
    ("관광", Preference.SIGHTSEEING),
    ("가볼만한곳", Preference.SIGHTSEEING),
    ("명소", Preference.SIGHTSEEING),
    ("공원", Preference.SIGHTSEEING),
    ("산책", Preference.REST),
    ("시장", Preference.SHOPPING),
    ("전통시장", Preference.SHOPPING),
    ("테마거리", Preference.SIGHTSEEING),
    ("박물관", Preference.SIGHTSEEING),
    ("미술관", Preference.SIGHTSEEING),
    ("전시", Preference.SIGHTSEEING),
    ("문화", Preference.SIGHTSEEING),
    ("전망", Preference.SIGHTSEEING),
    ("체험", Preference.EXPERIENCE),
    ("수목원", Preference.SIGHTSEEING),
    ("카페", Preference.REST),
)


def search_schedule_support(kakao, trip: TripRequest, anchor, *,
                            exclude_ids: frozenset[str] = frozenset(),
                            include_cafe: bool = True,
                            limit: int = 24) -> list[PlaceCandidate]:
    """Build schedule-support pool from Kakao keyword search; never reuse MAIN food bias.

    Preference on the trip only affects cafe inclusion soft-policy, not whether
    parks/markets/museums are queried.
    """
    found: dict[str, PlaceCandidate] = {}
    terms = SUPPORT_SEARCH_TERMS
    if not include_cafe:
        terms = tuple(row for row in terms if row[1] != Preference.REST or "카페" not in row[0])
    for term, preference in terms:
        query = f"{trip.destination} {term}"
        try:
            rows = kakao.search_places(query, page=1, size=12)
        except ProviderError:
            logger.warning("support_search outcome=failed query=%s", term)
            continue
        for row in rows:
            address = row.get("address_name") or row.get("road_address_name") or ""
            if not isinstance(address, str) or not belongs_to_destination(trip.destination, address):
                continue
            try:
                candidate = convert_place(row, anchor, preference)
            except (ValueError, TypeError):
                continue
            if candidate.place_id in exclude_ids:
                continue
            if is_micro_landmark(candidate):
                continue
            if not candidate.address or not candidate.category:
                continue
            # Schedule support places are full day-stop candidates, not radius-clipped NEARBY.
            candidate = candidate.model_copy(update={"role": "MAIN_DESTINATION"})
            old = found.get(candidate.place_id)
            if old is None:
                found[candidate.place_id] = candidate.model_copy(
                    update={"preference_score": discovery_score(candidate)})
                continue
            matches = tuple(dict.fromkeys(old.matched_preferences + (preference,)))
            queries = tuple(dict.fromkeys(old.matched_queries + (query,)))
            merged = old.model_copy(update={"matched_preferences": matches, "matched_queries": queries})
            found[candidate.place_id] = merged.model_copy(
                update={"preference_score": discovery_score(merged)})
    ranked = sorted(found.values(), key=lambda c: (-c.preference_score, c.place_id))
    logger.info("support_search count=%d destination=%s", len(ranked), trip.destination)
    return ranked[:limit]
