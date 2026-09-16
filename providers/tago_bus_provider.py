from typing import Any, Literal
from datetime import date

from providers.http_client import HttpClient, ProviderError
from providers.tago_base import TagoBase


class TagoBusProvider(TagoBase):
    def __init__(self, api_key: str, http: HttpClient,
                 kind: Literal["express", "intercity"] = "express") -> None:
        if kind not in {"express", "intercity"}:
            raise ValueError("Unsupported bus kind")
        super().__init__(api_key, http,
                         "ExpBusInfo" if kind == "express" else "SuburbsBusInfo")
        self.kind = kind

    def list_cities(self) -> list[dict[str, Any]]:
        return self.get_items("GetCtyCodeList")

    def list_terminals(self, keyword: str) -> list[dict[str, Any]]:
        operation = "GetExpBusTrminlList" if self.kind == "express" else "GetSuberbsBusTrminlList"
        return self.get_all_items(operation, terminalNm=keyword)

    def search_trips(self, departure_id: str, arrival_id: str,
                     travel_date: date) -> list[dict[str, Any]]:
        operation = ("GetStrtpntAlocFndExpbusInfo" if self.kind == "express"
                     else "GetStrtpntAlocFndSuberbsBusInfo")
        return self.get_all_items(operation, depTerminalId=departure_id,
                                  arrTerminalId=arrival_id,
                                  depPlandTime=travel_date.strftime("%Y%m%d"))

    def healthcheck(self) -> None:
        if not self.list_cities():
            raise ProviderError("no_data")
