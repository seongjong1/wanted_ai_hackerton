"""API-backed transport data. All datetimes represent Korean local time."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator
from models.access import BoardingAssessment

KST = ZoneInfo("Asia/Seoul")


class TransportType(str, Enum):
    TRAIN = "TRAIN"
    EXPRESS_BUS = "EXPRESS_BUS"
    INTERCITY_BUS = "INTERCITY_BUS"


class TransportCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, extra="forbid")
    transport_type: TransportType
    departure_place: str = Field(min_length=1)
    arrival_place: str = Field(min_length=1)
    departure_time: datetime
    arrival_time: datetime
    price: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    grade: str | None = None
    route_id: str | None = None
    train_number: str | None = None
    provider: str = Field(min_length=1)
    raw_data: dict[str, Any] = Field(default_factory=dict, repr=False, exclude=True)
    access: BoardingAssessment | None = None

    @model_validator(mode="after")
    def valid_times(self) -> Self:
        if self.departure_time.tzinfo is None or self.arrival_time.tzinfo is None:
            raise ValueError("Transport datetimes must include a timezone")
        if self.arrival_time <= self.departure_time:
            raise ValueError("Arrival must follow departure")
        return self

    @computed_field
    @property
    def duration_minutes(self) -> float:
        return (self.arrival_time - self.departure_time).total_seconds() / 60


@dataclass(frozen=True)
class TransportHub:
    id: str
    name: str


@dataclass(frozen=True)
class HubMatch:
    hubs: tuple[TransportHub, ...]
    detail: str
    limited: bool = False


@dataclass(frozen=True)
class TransportStatus:
    transport_type: TransportType
    status: str
    detail: str
    departure_match: HubMatch | None = None
    arrival_match: HubMatch | None = None


@dataclass
class TransportResult:
    candidates: list[TransportCandidate] = field(default_factory=list)
    by_type: dict[TransportType, list[TransportCandidate]] = field(default_factory=dict)
    statuses: list[TransportStatus] = field(default_factory=list)
    access_checked: bool = False
    origin_status: str = ""
    user_message: str = ""

    @property
    def origin_resolution_status(self) -> str:
        return {"DIRECT_HUB": "RESOLVED", "LOCAL_ORIGIN": "RESOLVED",
                "AMBIGUOUS_ORIGIN": "AMBIGUOUS", "NEED_ADDRESS": "NEED_ADDRESS",
                "INVALID_ORIGIN": "INVALID"}.get(self.origin_status, "UNAVAILABLE")
