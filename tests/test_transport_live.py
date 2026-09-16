"""Opt-in API smoke test. No network request occurs in the default test suite."""
import os
from datetime import datetime

import pytest

from config import load_settings
from models.transport import KST
from providers.http_client import HttpClient
from providers.tago_bus_provider import TagoBusProvider
from providers.tago_train_provider import TagoTrainProvider

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_TRANSPORT_TESTS") != "1",
                                reason="Opt-in real API test; consumes quota")


def test_live_catalog_and_schedule():
    settings = load_settings()
    if not settings.data_go_kr_api_key:
        pytest.skip("Set DATA_GO_KR_API_KEY in the environment")
    http = HttpClient(max_attempts=2)
    try:
        train = TagoTrainProvider(settings.data_go_kr_api_key, http)
        cities = train.list_cities()
        assert cities
        # Deliberately test retrieval, not route availability on any given day.
        for kind in ("express", "intercity"):
            bus = TagoBusProvider(settings.data_go_kr_api_key, http, kind)
            departure, arrival = bus.list_terminals("서울"), bus.list_terminals("부산")
            assert departure and arrival
            rows = bus.search_trips(str(departure[0]["terminalId"]), str(arrival[0]["terminalId"]),
                                    datetime.now(KST).date())
            assert isinstance(rows, list)
    finally:
        http.close()
