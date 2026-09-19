"""Destination Resolver — Kakao-grounded scopes (no LLM / no city hardcoding)."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from config import load_settings
from models.access import AccessPoint
from models.destination import ResolutionType
from services.destination_resolver import (
    clear_destination_cache,
    destination_cache_size,
    matches_resolved_destination,
    parse_admin_scope,
    resolve_destination,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_destination_cache()
    yield
    clear_destination_cache()


def _kakao(place_rows=None, address_rows=None):
    kakao = Mock()
    kakao.search_places.return_value = place_rows or []
    kakao.search_addresses.return_value = address_rows if address_rows is not None else []
    return kakao


def _addr(name, lat, lon):
    return {
        "address_name": name,
        "x": str(lon),
        "y": str(lat),
        "address": {
            "address_name": name,
            "x": str(lon),
            "y": str(lat),
        },
    }


def _row(pid, name, address, lat, lon, category="음식점"):
    return {
        "id": str(pid),
        "place_name": name,
        "address_name": address,
        "road_address_name": address,
        "x": str(lon),
        "y": str(lat),
        "category_name": category,
    }


def _masan_rows():
    return [
        _row(1, "마산어시장", "경남 창원시 마산합포구 수산동", 35.197, 128.568),
        _row(2, "마산로봇랜드", "경남 창원시 마산회원구 내서읍", 35.252, 128.515),
        _row(3, "창동예술촌", "경남 창원시 마산합포구 창동", 35.204, 128.575),
        _row(4, "합성동카페", "경남 창원시 마산회원구 합성동", 35.234, 128.580),
        _row(5, "무관한부산집", "부산 중구 중앙동", 35.100, 129.030),
    ]


def _masan_addresses():
    return [
        _addr("경남 창원시 마산합포구", 35.197, 128.568),
        _addr("경남 창원시 마산회원구", 35.221, 128.580),
    ]


def _gumi_rows():
    return [
        _row(1, "구미역앞", "경북 구미시 원평동", 36.128, 128.331),
        _row(2, "금오산", "경북 구미시 남통동", 36.120, 128.300),
        _row(3, "구미동분당", "경기 성남시 분당구 구미동", 37.340, 127.110),
    ]


def test_masan_resolves_multi_admin():
    kakao = _kakao(place_rows=_masan_rows(), address_rows=_masan_addresses())
    hub = AccessPoint(id="masan", name="마산역", x=128.57, y=35.20)
    resolved = resolve_destination(kakao, "마산", hub=hub)
    assert resolved.is_resolved
    assert resolved.resolution_type == ResolutionType.MULTI_ADMIN_REGION
    assert resolved.display_name == "마산"
    assert resolved.source == "KAKAO_ADDRESS"
    districts = {s.district for s in resolved.administrative_scopes}
    assert districts == {"마산합포", "마산회원"}
    assert all(s.city == "창원" for s in resolved.administrative_scopes)


def test_masan_candidates_pass_fail():
    kakao = _kakao(address_rows=_masan_addresses())
    resolved = resolve_destination(kakao, "마산")
    assert matches_resolved_destination(
        resolved, "경남 창원시 마산합포구 수산동", latitude=35.197, longitude=128.568)
    assert matches_resolved_destination(
        resolved, "경남 창원시 마산회원구 합성동", latitude=35.234, longitude=128.580)
    assert not matches_resolved_destination(
        resolved, "부산 중구 중앙동", latitude=35.100, longitude=129.030)


def test_gumi_admin_region():
    kakao = _kakao(
        place_rows=_gumi_rows(),
        address_rows=[
            _addr("경기 성남시 분당구 구미동", 37.34, 127.11),
            _addr("경북 구미시", 36.12, 128.33),
        ])
    resolved = resolve_destination(kakao, "구미")
    assert resolved.resolution_type == ResolutionType.ADMIN_REGION
    assert resolved.administrative_scopes[0].city == "구미"
    assert matches_resolved_destination(resolved, "경북 구미시 원평동")
    assert not matches_resolved_destination(resolved, "경기 성남시 분당구 구미동")


def test_seoul_admin_region():
    kakao = _kakao(address_rows=[_addr("서울", 37.566, 126.978)])
    resolved = resolve_destination(kakao, "서울")
    assert resolved.resolution_type == ResolutionType.ADMIN_REGION
    assert resolved.administrative_scopes[0].city == "서울"


def test_haeundae_metro_district():
    kakao = _kakao(address_rows=[_addr("부산 해운대구", 35.158, 129.160)])
    resolved = resolve_destination(kakao, "해운대")
    assert resolved.resolution_type == ResolutionType.ADMIN_REGION
    assert resolved.administrative_scopes[0].district == "해운대"


def test_hongdae_area_center_without_address_substring():
    kakao = _kakao(place_rows=[
        _row(1, "홍대입구역", "서울 마포구 동교동", 37.557, 126.924, "교통,수송 > 지하철역"),
        _row(2, "홍대놀이터", "서울 마포구 서교동", 37.556, 126.923),
        _row(3, "연남동카페", "서울 마포구 연남동", 37.566, 126.925),
    ], address_rows=[])
    resolved = resolve_destination(kakao, "홍대", area_radius_m=3500)
    assert resolved.resolution_type == ResolutionType.AREA_CENTER
    assert resolved.radius_m == 3500
    assert "홍대" not in "서울 마포구 서교동"
    assert matches_resolved_destination(
        resolved, "서울 마포구 서교동", latitude=37.556, longitude=126.923)
    assert not matches_resolved_destination(
        resolved, "서울 강남구 역삼동", latitude=37.500, longitude=127.036)


def test_seongsu_area_center():
    kakao = _kakao(
        address_rows=[
            _addr("서울 성동구 성수동1가", 37.544, 127.056),
            _addr("서울 성동구 성수동2가", 37.544, 127.056),
        ])
    resolved = resolve_destination(kakao, "성수")
    assert resolved.resolution_type == ResolutionType.AREA_CENTER
    assert matches_resolved_destination(
        resolved, "서울 성동구 성수동1가", latitude=37.544, longitude=127.056)


def test_gangnam_cluster_not_name_noise():
    kakao = _kakao(address_rows=[_addr("서울 강남구", 37.497, 127.028)])
    hub = AccessPoint(id="gn", name="강남역", x=127.028, y=37.497)
    resolved = resolve_destination(kakao, "강남", hub=hub)
    assert resolved.is_resolved
    assert matches_resolved_destination(
        resolved, "서울 강남구 역삼동", latitude=37.497, longitude=127.028)
    assert not matches_resolved_destination(
        resolved, "부산 해운대구 우동", latitude=35.163, longitude=129.163)


def test_resolver_cache_hit():
    kakao = _kakao(address_rows=[_addr("경북 구미시", 36.12, 128.33)])
    a = resolve_destination(kakao, "구미")
    b = resolve_destination(kakao, "구미")
    assert a is b
    assert kakao.search_addresses.call_count == 1
    assert destination_cache_size() == 1


def test_destination_change_uses_new_key():
    kakao = Mock()
    kakao.search_addresses.side_effect = [
        [_addr("경북 구미시", 36.12, 128.33)],
        _masan_addresses(),
    ]
    kakao.search_places.return_value = []
    resolve_destination(kakao, "구미")
    resolve_destination(kakao, "마산")
    assert kakao.search_addresses.call_count == 2
    assert destination_cache_size() == 2


def test_ambiguous_fails_softly():
    kakao = _kakao(place_rows=[
        _row(1, "행복카페서울", "서울 종로구", 37.57, 126.98),
        _row(2, "행복식당부산", "부산 중구", 35.10, 129.03),
        _row(3, "행복마트대구", "대구 중구", 35.87, 128.60),
        _row(4, "행복분식광주", "광주 동구", 35.15, 126.92),
    ], address_rows=[])
    resolved = resolve_destination(kakao, "행복")
    assert resolved.status in {"AMBIGUOUS", "FAILED"}
    assert not resolved.is_resolved
    assert "구체적으로" in resolved.notice


def test_no_masan_hardcoding_in_resolver_source():
    from pathlib import Path
    src = Path("services/destination_resolver.py").read_text(encoding="utf-8")
    assert 'destination == "마산"' not in src
    assert '== "마산"' not in src


def test_activity_and_destination_radius_independent():
    settings = load_settings({}, {
        "EXTENDED_ACTIVITY_RADIUS_METERS": "2000",
        "DESTINATION_SCOPE_RADIUS_METERS": "3500",
    })
    assert settings.extended_activity_radius_meters == 2000
    assert settings.destination_scope_radius_meters == 3500
    from services.place_service import activity_radius_meters
    assert activity_radius_meters("500m") == 500
    assert activity_radius_meters("500m 이상", settings.extended_activity_radius_meters) == 2000


def test_parse_admin_scope_changwon():
    scope = parse_admin_scope("경남 창원시 마산합포구 수산동")
    assert scope is not None
    assert scope.province == "경남" and scope.city == "창원" and scope.district == "마산합포"


def test_direct_place_station_suffix():
    kakao = _kakao(place_rows=[
        _row(1, "서울역", "서울 중구 봉래동2가", 37.555, 126.971, "교통,수송 > 기차역"),
        _row(2, "서울역광장", "서울 중구 봉래동2가", 37.555, 126.972),
        _row(3, "남대문", "서울 중구 남대문로", 37.560, 126.977),
    ], address_rows=[])
    resolved = resolve_destination(kakao, "서울역", area_radius_m=3500)
    assert resolved.is_resolved
    assert resolved.resolution_type in {
        ResolutionType.DIRECT_PLACE, ResolutionType.ADMIN_REGION, ResolutionType.AREA_CENTER}


def test_place_and_accommodation_share_resolver():
    """Same cache key / resolved object for Place + Accommodation layers."""
    kakao = _kakao(address_rows=_masan_addresses())
    hub = AccessPoint(id="m", name="마산역", x=128.57, y=35.20)
    place_resolved = resolve_destination(kakao, "마산", hub=hub, area_radius_m=3500)
    lodge_resolved = resolve_destination(kakao, "마산", hub=hub, area_radius_m=3500)
    assert place_resolved is lodge_resolved
    assert kakao.search_addresses.call_count == 1
