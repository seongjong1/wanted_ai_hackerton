"""Kakao public transit routing. Durations in this API are seconds."""
from __future__ import annotations

import logging
from collections import OrderedDict

from pydantic import ValidationError

from models.access import AccessPoint, AccessRoute
from providers.http_client import HttpClient, ProviderError, require_key
from services.kakao_route_steps import parse_access_step

logger = logging.getLogger("travel_ai.kakao_transit")

# Process-wide cache shared by Outbound Access, Schedule Gap Fill, and Multi-day
# (Streamlit reruns create new provider instances but keep the same Python process).
_ROUTE_CACHE: OrderedDict[tuple, AccessRoute | str] = OrderedDict()
_ROUTE_CACHE_MAX = 256


def clear_route_cache() -> None:
    _ROUTE_CACHE.clear()


def route_cache_size() -> int:
    return len(_ROUTE_CACHE)


def _cache_key(api_key: str, origin: AccessPoint, destination: AccessPoint) -> tuple:
    # Round coords so tiny float noise does not bypass reuse.
    return (api_key[-12:], round(origin.x, 5), round(origin.y, 5),
            round(destination.x, 5), round(destination.y, 5))


class KakaoTransitProvider:
    def __init__(self, api_key: str, http: HttpClient) -> None:
        self._api_key = api_key
        self.http = http

    def fastest_route(self, origin: AccessPoint, destination: AccessPoint) -> AccessRoute:
        require_key(self._api_key)
        key = _cache_key(self._api_key, origin, destination)
        cached = _ROUTE_CACHE.get(key)
        if cached is not None:
            _ROUTE_CACHE.move_to_end(key)
            logger.info("route_cache outcome=hit")
            if isinstance(cached, str):
                raise ProviderError(cached)
            return cached
        try:
            payload = self.http.request_json(
                "GET", "https://dapi.kakao.com/v2/routing/publictraffic", provider="kakao_transit",
                headers={"Authorization": f"KakaoAK {self._api_key}"},
                params={"start_x": origin.x, "start_y": origin.y,
                        "end_x": destination.x, "end_y": destination.y},
            )
            if payload.get("status") != "OK":
                raise ProviderError("ACCESS_TIME_UNKNOWN")
            raw_routes = payload.get("routes")
            if not isinstance(raw_routes, list):
                raise ProviderError("ACCESS_TIME_UNKNOWN")
            routes = []
            for row in raw_routes:
                try:
                    properties = row["properties"]
                    parsed = []
                    for step in row["steps"]:
                        access_step = parse_access_step(step)
                        if access_step is not None:
                            parsed.append(access_step)
                    steps = tuple(parsed)
                    if not steps:
                        continue
                    routes.append(AccessRoute(duration_seconds=properties["totalTime"],
                                              distance_meters=properties["totalDistance"],
                                              transfers=properties["transfers"], steps=steps))
                    # Note: Kakao totalTime often exceeds sum(step.time). The gap is usually
                    # station access / egress / wait not exposed as separate steps. Schedule
                    # timing must keep totalTime; UI lists steps as returned without inventing
                    # filler minutes.
                except (KeyError, TypeError, ValidationError):
                    continue  # Malformed alternative does not invalidate a valid route.
            if not routes:
                raise ProviderError("ACCESS_TIME_UNKNOWN")
            route = min(routes, key=lambda r: (r.duration_seconds, r.transfers, r.distance_meters))
            self._store(key, route)
            logger.info("route_cache outcome=store")
            return route
        except ProviderError as exc:
            # Cache failures (including quota) so Streamlit reruns do not re-hit the API.
            self._store(key, exc.code)
            logger.info("route_cache outcome=store_error code=%s", exc.code)
            raise

    @staticmethod
    def _store(key: tuple, value: AccessRoute | str) -> None:
        _ROUTE_CACHE[key] = value
        _ROUTE_CACHE.move_to_end(key)
        while len(_ROUTE_CACHE) > _ROUTE_CACHE_MAX:
            _ROUTE_CACHE.popitem(last=False)
