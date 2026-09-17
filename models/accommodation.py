"""Lodging recommendation models — distinct from MAIN place anchors."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from models.access import AccessPoint


class AccommodationCandidate(BaseModel):
    """Real Kakao lodging POI only — never invent price/rating/inventory."""
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    place_id: str = Field(min_length=1)
    place_name: str = Field(min_length=1)
    address: str = ""
    category: str = ""
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    distance_to_hub_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    route_to_hub_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    score: float = Field(default=0, allow_inf_nan=False)
    reason: str = ""
    provider: Literal["Kakao Local"] = "Kakao Local"

    def as_access_point(self) -> AccessPoint:
        return AccessPoint(
            id=self.place_id, name=self.place_name, address=self.address,
            category=self.category, x=self.longitude, y=self.latitude,
            source="kakao_local", original_query=self.place_name)


class AccommodationStay(BaseModel):
    """Future per-night lodging; Phase 4.8 applies one stay to every overnight."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    night_date: date | None = None  # None ⇒ applies to all nights in the trip
    point: AccessPoint


class AccommodationRecommendResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["ok", "empty", "failed"] = "empty"
    candidates: tuple[AccommodationCandidate, ...] = ()
    notices: tuple[str, ...] = ()
    search_count: int = 0
    route_eval_count: int = 0
