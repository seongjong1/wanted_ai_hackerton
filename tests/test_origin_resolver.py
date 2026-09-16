from unittest.mock import Mock

import pytest

from providers.http_client import ProviderError
from providers.kakao_provider import KakaoProvider
from services.origin_service import OriginResolver
from test_local_origin import setup


def row(name, id="1", address="서울 서초구 서초동 1", x=127.03, category="부동산 > 건물"):
    return {"id":id,"place_name":name,"address_name":address,"x":x,"y":37.48,"category_name":category}


def resolver(rows):
    provider=Mock()
    provider.search_places.return_value=rows
    provider.search_addresses.return_value=[]
    return OriginResolver(provider),provider


def test_yangjae_cluster_and_stable_original_name():
    rows=[row("양재역 3호선",category="교통 > 지하철역"),
          row("양재역 신분당선",id="2",x=127.031,category="교통 > 지하철역")]
    service,provider=resolver(rows)
    point=service.resolve("양재역")
    assert point.name=="양재역 3호선" and point.id=="1"
    assert point.original_query=="양재역" and point.source=="KAKAO_KEYWORD"
    provider.search_places.return_value=list(reversed(rows))
    assert service.resolve("양재역")==point
    provider.search_addresses.assert_not_called()


@pytest.mark.parametrize("name,category", [("검증빌딩","부동산"),("검증호텔","숙박"),
    ("검증오피스텔","주거시설"),("검증회사","기업"),("검증상점","쇼핑")])
def test_general_origin_and_access_leg(name,category):
    service,trip,places,_,_,_=setup(name)
    original=places.search_places.side_effect
    places.search_places.side_effect=lambda query,**kw: [row(name,category=category)] if query==name else original(query,**kw)
    result=service.search(trip)
    assert result.origin_status=="LOCAL_ORIGIN" and result.candidates
    assert all(c.access.access_leg.origin==name for c in result.candidates)


def test_qualified_building_over_duplicate_names():
    service,_=resolver([row("검증빌딩"),row("검증빌딩",id="2",address="부산 해운대구 우동 1")])
    assert service.resolve("서울특별시 서초구 검증빌딩").id=="1"
    with pytest.raises(ProviderError,match="AMBIGUOUS_ORIGIN"):
        service.resolve("검증빌딩")


@pytest.mark.parametrize("variant", ["far", "region", "category"])
def test_station_cluster_requires_evidence(variant):
    a=row("양재역 3호선",category="교통 > 지하철역")
    b=row("양재역 신분당선",id="2",category="교통 > 지하철역")
    if variant=="far": b["x"]=128
    if variant=="region": b["address_name"]="부산 해운대구 우동"
    if variant=="category": a["place_name"]=b["place_name"]="검증빌딩";a["category_name"]=b["category_name"]="건물"
    service,_=resolver([a,b])
    with pytest.raises(ProviderError,match="AMBIGUOUS_ORIGIN"):
        service.resolve("검증빌딩" if variant=="category" else "양재역")


@pytest.mark.parametrize("query", ["우리집","집","회사","현재 위치"])
def test_generic_input_is_not_geocoded(query):
    service,provider=resolver([row(query)])
    with pytest.raises(ProviderError,match="INVALID_ORIGIN"):
        service.resolve(query)
    provider.search_places.assert_not_called()


def test_address_fallback_without_poi_id_and_transport():
    query="서울특별시 강남구 테헤란로 123"
    service,trip,places,_,_,_=setup(query)
    original=places.search_places.side_effect
    places.search_places.side_effect=lambda q,**kw: [] if q==query else original(q,**kw)
    places.search_addresses.return_value=[{"address_type":"ROAD_ADDR","address_name":query,"x":127.03,"y":37.48}]
    result=service.search(trip)
    assert result.origin_status=="LOCAL_ORIGIN" and result.candidates
    point=result.candidates[0].access.access_leg.origin_point
    assert point.id.startswith("address:") and point.source=="KAKAO_ADDRESS"
    assert places.search_addresses.call_count==1


def test_address_failures_and_centroid_rejection():
    service,provider=resolver([])
    provider.search_addresses.return_value=[{"address_type":"REGION","address_name":"서울","x":127,"y":37}]
    with pytest.raises(ProviderError,match="INVALID_ORIGIN"): service.resolve_address("서울")
    provider.search_addresses.side_effect=ProviderError("http_429")
    with pytest.raises(ProviderError,match="http_429"): service.resolve_address("검증주소")


def test_keyword_outage_can_use_valid_address():
    service,provider=resolver([])
    provider.search_places.side_effect=ProviderError("timeout")
    provider.search_addresses.return_value=[{"address_type":"REGION_ADDR","address_name":"서울 서초구 서초동 1","x":127,"y":37}]
    assert service.resolve("서울 서초구 서초동 1").source=="KAKAO_ADDRESS"


def test_yangjae_without_tago_station_still_generates_transport():
    service,trip,places,_,_,_=setup("양재역")
    original=places.search_places.side_effect
    places.search_places.side_effect=lambda q,**kw: [row("양재역 3호선",category="교통 > 지하철역"),
        row("양재역 신분당선",id="2",category="교통 > 지하철역")] if q==trip.departure else original(q,**kw)
    result=service.search(trip)
    assert result.origin_status=="LOCAL_ORIGIN" and result.candidates


def test_address_provider_contract_and_invalid_payload():
    http=Mock()
    http.request_json.return_value={"documents":[]}
    provider=KakaoProvider("test-key",http)
    assert provider.search_addresses("도로명 1")==[]
    args,kwargs=http.request_json.call_args
    assert args==("GET","https://dapi.kakao.com/v2/local/search/address.json")
    assert kwargs["params"]=={"query":"도로명 1","analyze_type":"exact","size":30}
    http.request_json.return_value={"documents":None}
    with pytest.raises(ProviderError,match="invalid_response"):provider.search_addresses("도로명 1")


def test_address_disambiguates_multiple_businesses_at_same_address():
    query="서울 서초구 서초대로 1"
    service,provider=resolver([row("상점A",address=query),row("상점B",id="2",address=query)])
    provider.search_addresses.return_value=[{"address_type":"ROAD_ADDR","address_name":query,"x":127,"y":37}]
    assert service.resolve(query).source=="KAKAO_ADDRESS"


def test_station_name_spacing_and_parentheses():
    service,_=resolver([row("양재역(서초구청)3호선",category="교통 > 지하철역")])
    assert service.resolve("양재역").name=="양재역(서초구청)3호선"
