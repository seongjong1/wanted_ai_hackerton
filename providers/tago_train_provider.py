from typing import Any
from datetime import date

from providers.http_client import HttpClient, ProviderError
from providers.tago_base import TagoBase


class TagoTrainProvider(TagoBase):
    def __init__(self, api_key: str, http: HttpClient) -> None:
        super().__init__(api_key, http, "TrainInfo")

    def list_cities(self) -> list[dict[str, Any]]:
        return self.get_items("GetCtyCodeList")

    def list_stations(self, city_code: str) -> list[dict[str, Any]]:
        return self.get_all_items("GetCtyAcctoTrainSttnList", cityCode=city_code)

    def search_trips(self, departure_id: str, arrival_id: str,
                     travel_date: date) -> list[dict[str, Any]]:
        return self.get_all_items("GetStrtpntAlocFndTrainInfo", depPlaceId=departure_id,
                                  arrPlaceId=arrival_id,
                                  depPlandTime=travel_date.strftime("%Y%m%d"))

    def healthcheck(self) -> None:
        if not self.list_cities():
            raise ProviderError("no_data")
