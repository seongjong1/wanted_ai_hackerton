from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from models.access import AccessPoint
from models.trip_request import Preference


class CompanionStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"


class AllergyStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    UNCONFIRMED = "UNCONFIRMED"
    UNKNOWN = "UNKNOWN"


class PlaceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    place_id: str = Field(min_length=1)
    place_name: str = Field(min_length=1)
    category: str = ""
    address: str = ""
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    phone: str | None = None
    place_url: str | None = None
    provider: Literal["Kakao Local"] = "Kakao Local"
    preference_score: int = Field(default=0, ge=0)
    matched_preferences: tuple[Preference, ...] = ()
    validation_status: Literal["API_FIELDS_VALIDATED"] = "API_FIELDS_VALIDATED"
    pet_status: CompanionStatus = CompanionStatus.UNKNOWN
    child_status: CompanionStatus = CompanionStatus.UNKNOWN
    allergy_status: AllergyStatus = AllergyStatus.UNKNOWN
    raw_data: dict[str, Any] = Field(default_factory=dict, repr=False, exclude=True)
    ai_reason: str | None = None
    role: Literal["MAIN_DESTINATION", "NEARBY_PLACE"] = "NEARBY_PLACE"
    matched_queries: tuple[str, ...] = ()


@dataclass
class PlaceResult:
    anchor: AccessPoint | None = None
    anchor_candidates: tuple[AccessPoint, ...] = ()
    candidates: list[PlaceCandidate] = field(default_factory=list)
    radius_meters: int = 0
    status: str = "미검색"
    notices: list[str] = field(default_factory=list)
    ai_status: str = "미사용"
    destination_scope: str = ""
