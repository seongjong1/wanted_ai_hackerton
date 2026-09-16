"""Conservative name matching against live TAGO catalogs; never manufacture IDs."""
import re
from dataclasses import dataclass
from typing import Any

from models.transport import HubMatch, TransportHub
from providers.http_client import ProviderError
from providers.kakao_provider import KakaoProvider
from providers.tago_bus_provider import TagoBusProvider
from providers.tago_train_provider import TagoTrainProvider


def text_field(row: dict[str, Any], name: str) -> str:
    value = next((v for k, v in row.items() if k.casefold() == name.casefold()), None)
    return str(value).strip() if value is not None else ""


def normalize(value: str) -> str:
    return re.sub(r"[\s()·ㆍ-]", "", value).casefold()


def region_name(value: str) -> str:
    aliases = {"경상북도": "경북", "경상남도": "경남", "전라북도": "전북",
               "전북특별자치도": "전북", "전라남도": "전남", "충청북도": "충북",
               "충청남도": "충남", "강원특별자치도": "강원", "강원도": "강원",
               "제주특별자치도": "제주", "제주도": "제주", "경기도": "경기"}
    if value in aliases:
        return aliases[value]
    return re.sub(r"(특별자치시|특별시|광역시|시|군|구)$", "", value)


def hub_name(value: str) -> str:
    return re.sub(r"(종합버스터미널|시외버스터미널|고속버스터미널|버스터미널|종합터미널|터미널|역)$",
                  "", normalize(value))


@dataclass(frozen=True)
class LocationContext:
    query: str
    province: str = ""
    city: str = ""
    ambiguous: bool = False


def resolve_context(query: str, kakao: KakaoProvider | None) -> LocationContext:
    if kakao is None:
        return LocationContext(query)
    try:
        documents = kakao.search_places(query, size=5)
    except ProviderError:
        # Exact TAGO city/name matches can still work without Kakao.
        return LocationContext(query)
    regions: set[tuple[str, str]] = set()
    for row in documents:
        address = text_field(row, "address_name").split()
        if len(address) < 2:
            continue
        province = region_name(address[0])
        city = region_name(address[1]) if province in {"경기", "경북", "경남", "전북", "전남",
                                                       "충북", "충남", "강원", "제주"} else province
        regions.add((province, city))
    if len(regions) == 1:
        province, city = next(iter(regions))
        return LocationContext(query, province, city)
    return LocationContext(query, ambiguous=len(regions) > 1)


class HubResolver:
    """Request-local catalog reuse. No user data or credentials enter global caches."""
    def __init__(self, max_hubs: int = 3) -> None:
        self.max_hubs = max_hubs
        self.origin_selector = None
        self.origin_context = None
        self._stations: dict[str, list[dict[str, Any]]] = {}
        self._cities: list[dict[str, Any]] | None = None

    def train(self, location: LocationContext, provider: TagoTrainProvider) -> HubMatch:
        if location.ambiguous:
            return HubMatch((), "여러 지역이 검색됩니다. 시·도와 역 이름을 함께 입력하세요.")
        if self._cities is None:
            self._cities = provider.list_cities()
        tokens = {region_name(hub_name(t)) for t in location.query.split()}
        province = location.province
        cities = [r for r in self._cities
                  if region_name(text_field(r, "cityname")) in tokens | ({province} if province else set())]
        if not cities:
            return HubMatch((), "철도 지역을 확인하지 못했습니다. 시·도와 역 이름을 함께 입력하세요.")
        rows: list[dict[str, Any]] = []
        for city in cities[:2]:
            code = text_field(city, "citycode")
            if not code:
                raise ProviderError("invalid_response")
            if code not in self._stations:
                self._stations[code] = provider.list_stations(code)
            rows.extend(self._stations[code])
        hubs = self._hubs(rows, "nodeid", "nodename")
        term = hub_name(location.query.split()[-1])
        exact = [h for h in hubs if hub_name(h.name) == term]
        if exact:
            return self._match(exact, "TAGO 역 이름 일치")
        if self.origin_selector is not None and location is self.origin_context:
            return self.origin_selector(hubs, True)
        if location.query.endswith("역"):
            return HubMatch((), "입력한 역을 해당 지역 TAGO 목록에서 확인하지 못했습니다.")
        city_name = location.city or region_name(location.query.split()[-1])
        regional = [h for h in hubs if hub_name(h.name).startswith(normalize(city_name))]
        return self._match(regional, "지역명과 일치하는 철도 거점")

    def bus(self, location: LocationContext, provider: TagoBusProvider) -> HubMatch:
        if location.ambiguous:
            return HubMatch((), "여러 지역이 검색됩니다. 시·도와 터미널 이름을 함께 입력하세요.")
        explicit = "터미널" in location.query
        term = hub_name(location.query.split()[-1])
        # A rail station or neighborhood may use terminals in its Kakao-confirmed city.
        local = self.origin_selector is not None and location is self.origin_context
        query = (location.city or region_name(term)) if local else (term if explicit else (location.city or region_name(term)))
        rows = provider.list_terminals(query)
        hubs = self._hubs(rows, "terminalId", "terminalNm")
        exact = [h for h in hubs if hub_name(h.name) == term]
        if exact:
            return self._match(exact, "TAGO 터미널 이름 일치")
        if explicit and not local:
            return HubMatch((), "터미널 이름을 TAGO 목록과 연결하지 못했습니다. 도시명으로 다시 조회하세요.")
        if self.origin_selector is not None and location is self.origin_context:
            return self.origin_selector(hubs, False)
        matches = [h for h in hubs if hub_name(h.name).startswith(normalize(query))]
        return self._match(matches, "지역명으로 탐색한 터미널 후보 (실제 거점 이름 확인 필요)")

    @staticmethod
    def _hubs(rows: list[dict[str, Any]], id_key: str, name_key: str) -> list[TransportHub]:
        result = {text_field(r, id_key): TransportHub(text_field(r, id_key), text_field(r, name_key))
                  for r in rows if text_field(r, id_key) and text_field(r, name_key)}
        if rows and not result:
            raise ProviderError("invalid_response")
        return sorted(result.values(), key=lambda h: (h.name, h.id))

    def _match(self, hubs: list[TransportHub], detail: str) -> HubMatch:
        if not hubs:
            return HubMatch((), "일치하는 교통 거점이 없습니다. 구체적인 역/터미널명을 입력하세요.")
        return HubMatch(tuple(hubs[:self.max_hubs]), detail, len(hubs) > self.max_hubs)
