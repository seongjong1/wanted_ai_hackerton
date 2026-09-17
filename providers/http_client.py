"""Bounded retries and sanitized provider failures shared by all adapters."""
import logging
import random
import time
from collections.abc import Callable
from typing import Any

import requests

logger = logging.getLogger("travel_ai.http")
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class HttpClient:
    def __init__(self, *, session: requests.Session | None = None,
                 max_attempts: int = 3, timeout: tuple[float, float] = (3.0, 8.0),
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not 1 <= max_attempts <= 5 or any(t <= 0 for t in timeout):
            raise ValueError("Invalid HTTP policy")
        self.session = session if session is not None else requests.Session()
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.sleep = sleep

    def close(self) -> None:
        self.session.close()

    def request_json(self, method: str, url: str, *, provider: str,
                     params: dict[str, Any] | None = None,
                     headers: dict[str, str] | None = None,
                     json: dict[str, Any] | None = None,
                     validate: Callable[[dict[str, Any]], None] | None = None,
                     retry: bool = True) -> dict[str, Any]:
        attempts = self.max_attempts if retry else 1
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            retry_after = 0.0
            try:
                with self.session.request(method, url, params=params, headers=headers,
                                          json=json, timeout=self.timeout,
                                          allow_redirects=False) as response:
                    status = response.status_code
                    if status >= 300:
                        raw_delay = response.headers.get("Retry-After", "0")
                        try:
                            retry_after = max(0.0, float(raw_delay))
                        except ValueError:
                            retry_after = 0.0
                        code = f"http_{status}"
                        # Detect Kakao Mobility quota without logging response bodies.
                        if status == 400:
                            try:
                                err = response.json()
                            except ValueError:
                                err = {}
                            if isinstance(err, dict) and (
                                    err.get("code") == -10
                                    or "limit" in str(err.get("message") or "").lower()):
                                code = "http_400_quota"
                        raise ProviderError(code,
                                            retryable=status in TRANSIENT_STATUSES)

                    try:
                        payload = response.json()
                    except ValueError:
                        # TAGO sometimes returns XML errors even with _type=json.
                        raise ProviderError("invalid_json") from None
                    if not isinstance(payload, dict):
                        raise ProviderError("invalid_response")
                    if validate:
                        validate(payload)
                logger.info("provider=%s attempt=%s outcome=success latency_ms=%d",
                            provider, attempt, (time.monotonic() - started) * 1000)
                return payload
            except requests.Timeout:
                error = ProviderError("timeout", retryable=True)
            except requests.ConnectionError:
                error = ProviderError("connection_error", retryable=True)
            except requests.RequestException:
                error = ProviderError("request_error")
            except ProviderError as exc:
                error = exc
            # Never log URL, headers, query, request/response body or exception text.
            logger.warning("provider=%s attempt=%s outcome=%s latency_ms=%d",
                           provider, attempt, error.code,
                           (time.monotonic() - started) * 1000)
            if not error.retryable or attempt == attempts or retry_after > 8:
                raise error from None
            self.sleep(max(retry_after, min(8.0, 0.5 * 2 ** (attempt - 1)
                                           + random.uniform(0, 0.2))))
        raise AssertionError("unreachable")


def require_key(api_key: str) -> None:
    if not api_key.strip():
        raise ProviderError("missing_key")
