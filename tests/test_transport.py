from datetime import date, time
from decimal import Decimal
from unittest.mock import Mock

import pytest

from models.transport import TransportType
from models.trip_request import TripRequest
from providers.http_client import ProviderError
from services.transport_service import TransportService, convert_candidate, rank_candidates

DAY = date(2026, 10, 1)


@pytest.fixture
def trip():
    return TripRequest(departure="서울", destination="부산", start_date=DAY, end_date=DAY,
                       departure_time=time(9), end_time=time(20), has_accommodation=False,
                       has_pet=False, has_child=False, activity_radius="500m", preferences=["관광"])


def train_row(departure="20261001093000", arrival="20261001120000", price="50000", **changes):
    return dict(depplacename="서울", arrplacename="부산", depplandtime=departure,
                arrplandtime=arrival, adultcharge=price, traingradename="KTX", trainno="1", **changes)


def bus_row():
    return dict(depPlaceNm="서울경부", arrPlaceNm="부산", depPlandTime="202610010940",
                arrPlandTime="202610011400", charge="30000", gradeNm="우등", routeId="r1")


def providers():
    train = Mock()
    train.list_cities.return_value = [{"citycode": "11", "cityname": "서울특별시"},
                                     {"citycode": "21", "cityname": "부산광역시"}]
    train.list_stations.side_effect = lambda city: ([{"nodeid": "S", "nodename": "서울"}]
                                                   if city == "11" else [{"nodeid": "B", "nodename": "부산"}])
    train.search_trips.return_value = [train_row()]
    express, intercity = Mock(), Mock()
    for provider in (express, intercity):
        provider.list_terminals.side_effect = lambda keyword: ([{"terminalId": "E1", "terminalNm": "서울경부"}]
                                                               if keyword == "서울" else [{"terminalId": "E2", "terminalNm": "부산"}])
        provider.search_trips.return_value = [bus_row()]
    return train, express, intercity


def test_duration_and_raw_hidden():
    candidate = convert_candidate(train_row(), TransportType.TRAIN)
    assert candidate.duration_minutes == 150
    assert candidate.price == Decimal("50000")
    assert "raw_data" not in candidate.model_dump()
    assert "depplandtime" not in repr(candidate)


def test_overnight_duration(trip):
    candidate = convert_candidate(train_row("20261001233000", "20261002013000"), TransportType.TRAIN)
    assert candidate.duration_minutes == 120
    overnight = trip.model_copy(update={"end_date": date(2026, 10, 2)})
    assert rank_candidates([candidate], overnight) == [candidate]
    assert rank_candidates([candidate], trip) == []


@pytest.mark.parametrize("dep,arr", [("20261001083000", "20261001120000"),
                                    ("20261002093000", "20261002120000")])
def test_time_filters(trip, dep, arr):
    assert rank_candidates([convert_candidate(train_row(dep, arr), TransportType.TRAIN)], trip) == []


def test_end_time_does_not_filter_outbound(trip):
    # Destination arrival after end_time is allowed; end_time is the home-return goal.
    late = convert_candidate(train_row("20261001093000", "20261001203000"), TransportType.TRAIN)
    assert rank_candidates([late], trip) == [late]


def test_arrival_before_departure_priority(trip):
    a = convert_candidate(train_row("20261001091000", "20261001130000"), TransportType.TRAIN)
    b = convert_candidate(train_row(), TransportType.TRAIN)
    assert rank_candidates([a, b], trip) == [b, a]


def test_duration_tiebreak_before_price(trip):
    a = convert_candidate(train_row("20261001090000", price="10000"), TransportType.TRAIN)
    b = convert_candidate(train_row(price="50000"), TransportType.TRAIN)
    assert rank_candidates([a, b], trip) == [b, a]


def test_price_tiebreak_and_unknown_last(trip):
    candidates = [convert_candidate({**train_row(price=p), "trainno": str(i)}, TransportType.TRAIN)
                  for i, p in enumerate(["", "50000", "30000"])]
    assert [c.price for c in rank_candidates(candidates, trip)] == [Decimal(30000), Decimal(50000), None]


def test_duplicate_schedule_with_conflicting_fare_deterministic(trip):
    a = convert_candidate(train_row(), TransportType.TRAIN)
    b = convert_candidate(train_row(price="30000"), TransportType.TRAIN)
    assert rank_candidates([a, a, b], trip) == [b]
    assert rank_candidates([b, a], trip) == [b]


@pytest.mark.parametrize("field,value", [("arrplandtime", ""), ("depplacename", ""),
                                        ("arrplacename", ""), ("arrplandtime", "20261001093000"),
                                        ("arrplandtime", "20261001080000"), ("arrplandtime", "25:30"),
                                        ("arrplandtime", "20260230110000")])
def test_invalid_required_fields(field, value):
    with pytest.raises(ValueError):
        convert_candidate({**train_row(), field: value}, TransportType.TRAIN)


@pytest.mark.parametrize("price", ["unknown", "-100", "NaN", "Infinity"])
def test_invalid_optional_price_becomes_unknown(price):
    assert convert_candidate(train_row(price=price), TransportType.TRAIN).price is None


def test_service_all_modes_success_and_code_lookup(trip):
    train, express, intercity = providers()
    result = TransportService(train, express, intercity, today=lambda: DAY).search(trip)
    assert len(result.candidates) == 3
    assert all(s.status == "정상" for s in result.statuses)
    train.search_trips.assert_called_once_with("S", "B", DAY)
    express.search_trips.assert_called_once_with("E1", "E2", DAY)
    assert result.candidates[0].transport_type == TransportType.TRAIN
    assert train.list_cities.call_count == 1


def test_empty_api_results(trip):
    values = providers()
    for provider in values:
        provider.search_trips.return_value = []
    result = TransportService(*values, today=lambda: DAY).search(trip)
    assert result.candidates == []
    assert all(s.status == "후보 없음" for s in result.statuses)


def test_partial_provider_failure(trip):
    values = providers()
    values[1].search_trips.side_effect = ProviderError("timeout")
    result = TransportService(*values, today=lambda: DAY).search(trip)
    assert len(result.candidates) == 2
    assert result.statuses[1].status == "조회 실패"
    assert result.statuses[0].status == result.statuses[2].status == "정상"


def test_intercity_future_date_skipped(trip):
    values = providers()
    result = TransportService(*values, today=lambda: date(2026, 9, 30)).search(trip)
    assert result.statuses[2].status == "날짜 제한"
    values[2].list_terminals.assert_not_called()
    values[2].search_trips.assert_not_called()


def test_malformed_rows_do_not_remove_good_rows(trip):
    values = providers()
    values[0].search_trips.return_value = [train_row(), {"depplandtime": "bad"}]
    result = TransportService(*values, today=lambda: DAY).search(trip)
    assert result.statuses[0].status == "일부 결과"
    assert len(result.by_type[TransportType.TRAIN]) == 1


def test_limits(trip):
    values = providers()
    values[0].search_trips.return_value = [{**train_row(), "trainno": str(i)} for i in range(20)]
    result = TransportService(*values, today=lambda: DAY).search(trip)
    assert len(result.by_type[TransportType.TRAIN]) == len(result.candidates) == 5


def test_no_hubs_no_trip_calls(trip):
    values = providers()
    values[0].list_stations.side_effect = lambda _: []
    result = TransportService(*values, today=lambda: DAY).search(trip)
    values[0].search_trips.assert_not_called()
    assert result.statuses[0].status == "매칭 실패"


def test_pair_failure_retains_prior_candidates(trip):
    values = providers()
    values[1].list_terminals.side_effect = lambda q: ([{"terminalId": "1", "terminalNm": "서울경부"},
                                                    {"terminalId": "2", "terminalNm": "서울호남"}]
                                                   if q == "서울" else [{"terminalId": "3", "terminalNm": "부산"}])
    values[1].search_trips.side_effect = [[bus_row()], ProviderError("http_503")]
    result = TransportService(*values, today=lambda: DAY).search(trip)
    assert len(result.by_type[TransportType.EXPRESS_BUS]) == 1
    assert result.statuses[1].status == "일부 결과"


def test_boundary_departure_and_arrival_included(trip):
    candidate = convert_candidate(train_row("20261001090000", "20261001200000"), TransportType.TRAIN)
    assert rank_candidates([candidate], trip) == [candidate]
