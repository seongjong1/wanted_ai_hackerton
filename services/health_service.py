from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic

from config import Settings
from providers.groq_provider import GroqProvider
from providers.http_client import HttpClient, ProviderError
from providers.kakao_provider import KakaoProvider
from providers.tago_bus_provider import TagoBusProvider
from providers.tago_train_provider import TagoTrainProvider


@dataclass(frozen=True)
class HealthResult:
    provider: str
    status: str
    detail: str
    latency_ms: int
    checked_at: str


def describe_error(code: str) -> str:
    if code == "missing_key":
        return "Secret을 설정한 후 다시 확인하세요."
    if code in {"http_401", "http_403", "tago_20", "tago_21", "tago_30", "tago_31", "tago_32"}:
        return "인증키 또는 API 활용신청/접근 권한을 확인하세요."
    if code in {"http_429", "tago_22", "tago_23"}:
        return "호출 한도를 초과했습니다. 잠시 후 또는 한도 초기화 후 확인하세요."
    return {
        "timeout": "응답 시간이 초과되었습니다. 잠시 후 다시 확인하세요.",
        "connection_error": "네트워크 연결에 실패했습니다.",
        "invalid_json": "JSON 응답이 아닙니다. 인증 설정과 제공기관 상태를 확인하세요.",
        "invalid_response": "API 응답 구조를 확인할 수 없습니다.",
        "no_data": "연결은 되었지만 점검용 조회 결과가 없습니다.",
        "model_unavailable": "지정 모델을 조회할 수 없습니다. 모델 접근 권한을 확인하세요.",
    }.get(code, "API 요청에 실패했습니다. 설정과 제공기관 상태를 확인하세요.")


def check_connections(settings: Settings, http: HttpClient | None = None) -> list[HealthResult]:
    client = http if http is not None else HttpClient(max_attempts=2)
    providers = [
        ("Kakao", KakaoProvider(settings.kakao_rest_api_key, client)),
        ("TAGO Train", TagoTrainProvider(settings.data_go_kr_api_key, client)),
        ("TAGO Bus · 고속", TagoBusProvider(settings.data_go_kr_api_key, client, "express")),
        ("TAGO Bus · 시외", TagoBusProvider(settings.data_go_kr_api_key, client, "intercity")),
        ("Groq", GroqProvider(settings.groq_api_key, client, settings.groq_model)),
    ]
    results = []
    try:
        for name, provider in providers:
            started = monotonic()
            try:
                provider.healthcheck()
                status, detail = "정상", "인증 및 점검용 조회 성공"
                if name == "Groq":
                    detail = "인증 및 모델 목록 확인 성공 (추론 호출 미검증)"
            except ProviderError as exc:
                status = "미설정" if exc.code == "missing_key" else "확인 필요"
                detail = describe_error(exc.code)
            results.append(HealthResult(name, status, detail, int((monotonic() - started) * 1000),
                                        datetime.now(timezone.utc).isoformat()))
    finally:
        if http is None:
            client.close()
    return results
