from typing import Any
from urllib.parse import unquote

from providers.http_client import HttpClient, ProviderError, require_key


class TagoBase:
    def __init__(self, api_key: str, http: HttpClient, service: str) -> None:
        self._api_key = unquote(api_key)  # requests performs exactly one query encoding.
        self.http = http
        self.service = service

    def get_items(self, operation: str, **params: Any) -> list[dict[str, Any]]:
        return self._extract_items(self._get_body(operation, **params))

    def get_all_items(self, operation: str, *, max_pages: int = 10,
                      page_size: int = 100, **params: Any) -> list[dict[str, Any]]:
        """Bounded pagination; an incomplete catalog must not appear complete."""
        if not 1 <= max_pages <= 20 or not 1 <= page_size <= 100:
            raise ValueError("Invalid pagination limits")
        result: list[dict[str, Any]] = []
        previous: list[dict[str, Any]] | None = None
        for page in range(1, max_pages + 1):
            body = self._get_body(operation, **params, pageNo=page, numOfRows=page_size)
            rows = self._extract_items(body)
            if rows and rows == previous:
                raise ProviderError("pagination_stalled")
            result.extend(rows)
            total = body.get("totalCount")
            if total is not None:
                try:
                    count = int(total)
                except (TypeError, ValueError):
                    raise ProviderError("invalid_response") from None
                if count < 0 or (not rows and len(result) < count):
                    raise ProviderError("invalid_response")
                if len(result) >= count:
                    return result
            elif len(rows) < page_size:
                return result
            previous = rows
        raise ProviderError("pagination_limit")

    def _get_body(self, operation: str, **params: Any) -> dict[str, Any]:
        require_key(self._api_key)
        data = self.http.request_json(
            "GET", f"https://apis.data.go.kr/1613000/{self.service}/{operation}",
            provider=self.service,
            params={**params, "serviceKey": self._api_key, "_type": "json"},
            validate=self._validate_envelope,
        )
        body = data["response"].get("body")
        if not isinstance(body, dict):
            raise ProviderError("invalid_response")
        return body

    @staticmethod
    def _extract_items(body: dict[str, Any]) -> list[dict[str, Any]]:
        items = body.get("items")
        if items in (None, ""):
            if str(body.get("totalCount")) == "0":
                return []
            raise ProviderError("invalid_response")
        if not isinstance(items, dict):
            raise ProviderError("invalid_response")
        rows = items.get("item", [])
        if isinstance(rows, dict):
            return [rows]
        if not isinstance(rows, list) or any(not isinstance(v, dict) for v in rows):
            raise ProviderError("invalid_response")
        return rows

    @staticmethod
    def _validate_envelope(data: dict[str, Any]) -> None:
        response = data.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("header"), dict):
            raise ProviderError("invalid_response")
        code = str(response["header"].get("resultCode", ""))
        if code not in {"0", "00", "0000"}:
            # Allowlisted codes only; upstream messages can echo credentials.
            known = {"01", "02", "03", "04", "05", "10", "12", "20", "21", "22",
                     "23", "29", "30", "31", "32", "99"}
            raise ProviderError(f"tago_{code}" if code in known else "tago_error",
                                retryable=code in {"01", "02", "04", "05", "23"})
