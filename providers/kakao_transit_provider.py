"""Kakao public transit routing. Durations in this API are seconds."""
from pydantic import ValidationError

from models.access import AccessPoint, AccessRoute, AccessStep
from providers.http_client import HttpClient, ProviderError, require_key


class KakaoTransitProvider:
    def __init__(self, api_key: str, http: HttpClient) -> None:
        self._api_key = api_key
        self.http = http

    def fastest_route(self, origin: AccessPoint, destination: AccessPoint) -> AccessRoute:
        require_key(self._api_key)
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
                steps = tuple(AccessStep(mode=step["properties"]["type"],
                                         duration_seconds=step["properties"]["time"],
                                         distance_meters=step["properties"]["distance"],
                                         guidance=step["properties"].get("guidance", ""))
                              for step in row["steps"])
                routes.append(AccessRoute(duration_seconds=properties["totalTime"],
                                          distance_meters=properties["totalDistance"],
                                          transfers=properties["transfers"], steps=steps))
            except (KeyError, TypeError, ValidationError):
                continue  # Malformed alternative does not invalidate a valid route.
        if not routes:
            raise ProviderError("ACCESS_TIME_UNKNOWN")
        return min(routes, key=lambda r: (r.duration_seconds, r.transfers, r.distance_meters))
