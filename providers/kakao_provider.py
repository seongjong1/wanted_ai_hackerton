from typing import Any

from providers.http_client import HttpClient, ProviderError, require_key


class KakaoProvider:
    def __init__(self, api_key: str, http: HttpClient) -> None:
        self._api_key = api_key
        self.http = http

    def search_places(self, query: str, *, x: float | None = None,
                      y: float | None = None, radius: int | None = None,
                      page: int = 1, size: int = 15) -> list[dict[str, Any]]:
        require_key(self._api_key)
        if not query.strip() or not 1 <= page <= 45 or not 1 <= size <= 15:
            raise ValueError("Invalid search query or pagination")
        if (x is None) != (y is None) or (radius is not None and x is None):
            raise ValueError("Radius search requires both coordinates")
        if x is not None and (not -180 <= x <= 180 or not -90 <= y <= 90):
            raise ValueError("Invalid coordinates")
        if radius is not None and not 0 <= radius <= 20000:
            raise ValueError("Radius must be between 0 and 20000 meters")
        params: dict[str, Any] = {"query": query.strip(), "page": page, "size": size}
        if x is not None:
            params.update(x=x, y=y)
        if radius is not None:
            params["radius"] = radius
        data = self.http.request_json(
            "GET", "https://dapi.kakao.com/v2/local/search/keyword.json",
            provider="kakao", params=params,
            headers={"Authorization": f"KakaoAK {self._api_key}"},
        )
        documents = data.get("documents")
        if not isinstance(documents, list) or any(not isinstance(v, dict) for v in documents):
            raise ProviderError("invalid_response")
        return documents

    def search_addresses(self, query: str) -> list[dict[str, Any]]:
        require_key(self._api_key)
        if not query.strip():
            raise ValueError("Empty address query")
        data = self.http.request_json(
            "GET", "https://dapi.kakao.com/v2/local/search/address.json",
            provider="kakao_address", params={"query": query.strip(), "analyze_type": "exact", "size": 30},
            headers={"Authorization": f"KakaoAK {self._api_key}"})
        rows = data.get("documents")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ProviderError("invalid_response")
        return rows

    def healthcheck(self) -> None:
        if not self.search_places("서울역", size=1):
            raise ProviderError("no_data")
