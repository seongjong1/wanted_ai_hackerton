from datetime import date, time
from unittest.mock import Mock

import pytest

from config import load_settings
from models.access import AccessPoint
from models.trip_request import Preference, TripRequest
from models.transport import TransportType
from providers.http_client import ProviderError
from services.place_service import (PlaceReasons, PlaceService, activity_radius_meters,
                                    add_ai_reasons, convert_place, rank_places)
from services.transport_service import convert_candidate


@pytest.fixture
def trip():
    return TripRequest(departure="서울역", destination="구미", start_date=date(2026,10,1),
                       end_date=date(2026,10,1), departure_time=time(9), end_time=time(20),
                       has_accommodation=False, has_pet=True, has_child=True, allergies=["땅콩"],
                       activity_radius="500m", preferences=["맛집","관광"])


@pytest.fixture
def selected():
    return convert_candidate({"depplacename":"서울","arrplacename":"구미","depplandtime":"20261001090000",
                              "arrplandtime":"20261001120000"},TransportType.TRAIN)


@pytest.fixture
def anchor():
    return AccessPoint(id="station",name="구미역",x=128.33,y=36.12)


def row(id="1", y=36.121, **extra):
    return {"id":id,"place_name":f"장소 {id}","x":"128.33","y":str(y),
            "category_name":"음식점","address_name":"경북 구미시", **extra}


def service(anchor, rows):
    kakao, resolver = Mock(), Mock()
    kakao.search_places.return_value = rows
    resolver.side_effect = lambda query: anchor if query == "구미역" else (_ for _ in ()).throw(ProviderError("no_data"))
    return PlaceService(kakao,resolve_point=resolver), kakao, resolver


@pytest.mark.parametrize("value,expected",[("100m",100),("200m",200),("300m",300),("400m",400),("500m",500),("500m 이상",2000)])
def test_radius(value,expected):
    assert activity_radius_meters(value) == expected


def test_extended_radius_config():
    settings=load_settings({}, {"EXTENDED_ACTIVITY_RADIUS_METERS":"3000"})
    assert activity_radius_meters("500m 이상",settings.extended_activity_radius_meters)==3000
    assert load_settings({}, {"EXTENDED_ACTIVITY_RADIUS_METERS":"bad"}).extended_activity_radius_meters==2000
    with pytest.raises(ValueError): activity_radius_meters("500m 이상",20001)


def test_anchor_coordinates_and_radius_in_request(trip,selected,anchor):
    s,kakao,resolver=service(anchor,[row()])
    result=s.search(trip,selected)
    assert resolver.call_args_list[0].args == ("구미역",)
    assert result.anchor==anchor
    assert kakao.search_places.call_args.kwargs["x"]==anchor.x
    assert kakao.search_places.call_args.kwargs["radius"]==500


def test_invalid_outside_duplicate_and_condition_unknown(trip,selected,anchor):
    s,_,_=service(anchor,[row(),row(),row("far",37),row("missing",x=None),row("nameless",place_name=" ")])
    result=s.search(trip,selected)
    assert len(result.candidates)==1
    candidate=result.candidates[0]
    assert candidate.preference_score == 73
    assert len(candidate.matched_queries) == 4
    assert set(candidate.matched_preferences) == {Preference.FOOD, Preference.SIGHTSEEING}
    assert candidate.pet_status==candidate.child_status==candidate.allergy_status=="UNKNOWN"
    assert "raw_data" not in candidate.model_dump()
    assert candidate.validation_status=="API_FIELDS_VALIDATED"


def test_preference_then_distance_ranking(trip,anchor):
    close=convert_place(row("close",36.1201),anchor,Preference.FOOD)
    far=convert_place(row("far",36.123),anchor,Preference.FOOD).model_copy(update={"preference_score":200})
    assert rank_places([close,far],trip)==[far,close]
    equal=far.model_copy(update={"preference_score":100})
    assert rank_places([equal,close],trip)==[close,equal]


def test_missing_distance_uses_coordinates_not_fake_api_distance(trip,selected,anchor):
    s,_,_=service(anchor,[row(distance="0",y=37)])
    assert s.search(trip,selected).candidates==[]


def test_zero_results(trip,selected,anchor):
    s,_,_=service(anchor,[])
    result=s.search(trip,selected)
    assert result.status=="후보 없음"
    assert not result.candidates


def test_provider_failure_and_partial_success(trip,selected,anchor):
    s,kakao,_=service(anchor,[])
    kakao.search_places.side_effect=[ProviderError("timeout"), [row()], [], []]
    result=s.search(trip,selected)
    assert len(result.candidates)==1 and result.status=="일부 결과"


def test_all_search_failures(trip,selected,anchor):
    s,kakao,_=service(anchor,[])
    kakao.search_places.side_effect=ProviderError("http_401")
    assert s.search(trip,selected).status=="조회 실패"


def test_unresolved_hub_not_silently_replaced(trip,selected,anchor):
    kakao,resolver=Mock(),Mock()
    resolver.side_effect=lambda q: anchor if q==trip.destination else (_ for _ in ()).throw(ProviderError("unknown"))
    result=PlaceService(kakao,resolve_point=resolver).search(trip,selected)
    assert result.anchor is None
    assert result.anchor_candidates==(anchor,)
    kakao.search_places.assert_not_called()


def test_specific_destination_anchor_option(trip,selected,anchor):
    target=AccessPoint(id="target",name="금오산",x=128.30,y=36.10)
    kakao,resolver=Mock(),Mock()
    kakao.search_places.return_value=[]
    resolver.side_effect=lambda q: anchor if q=="구미역" else target
    result=PlaceService(kakao,resolve_point=resolver).search(trip,selected,"target")
    assert result.anchor==target and result.anchor_candidates==(anchor,target)
    assert kakao.search_places.call_args.kwargs["x"]==target.x


@pytest.mark.parametrize("choice",[
    {"place_id":"nonexistent","preference":"맛집","reason_code":"PREFERENCE_MATCH"},
    {"place_id":"1","preference":"야경","reason_code":"PREFERENCE_MATCH"},
    {"place_id":"1","preference":"맛집","reason_code":"MULTIPLE_PREFERENCES"},
])
def test_ungrounded_ai_rejected(anchor,choice):
    candidate=convert_place(row(),anchor,Preference.FOOD)
    groq=Mock()
    groq.generate_structured.return_value=PlaceReasons(choices=[choice])
    candidates,status=add_ai_reasons([candidate],groq)
    assert candidates==[candidate]
    assert candidates[0].ai_reason is None
    assert "미사용" in status


def test_ai_failure_fallback_and_top_ten_only(anchor):
    candidates=[convert_place(row(str(i)),anchor,Preference.FOOD) for i in range(15)]
    groq=Mock()
    groq.generate_structured.side_effect=ProviderError("timeout")
    result,_=add_ai_reasons(candidates,groq)
    assert result==candidates
    assert len(groq.generate_structured.call_args.args[1]["candidates"])==10


def test_grounded_ai_reason(anchor):
    candidate=convert_place(row(),anchor,Preference.FOOD)
    groq=Mock()
    groq.generate_structured.return_value=PlaceReasons(choices=[{"place_id":"1","preference":"맛집","reason_code":"NEAR_ANCHOR"}])
    result,status=add_ai_reasons([candidate],groq)
    assert status=="검증 완료"
    assert "음식점 카테고리" in result[0].ai_reason
    assert "직선거리 약 111m" in result[0].ai_reason


def test_no_ai_call_when_no_places(trip,selected,anchor):
    s,_,_=service(anchor,[])
    s.groq=Mock()
    s.search(trip,selected)
    s.groq.generate_structured.assert_not_called()
