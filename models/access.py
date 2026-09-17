"""Access movement is separate from the long-distance transport candidate."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class AccessPoint(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str = ""
    category: str = ""
    original_query: str = ""
    source: str = ""
    x: float = Field(ge=-180, le=180, allow_inf_nan=False)
    y: float = Field(ge=-90, le=90, allow_inf_nan=False)


class AccessStep(BaseModel):
    model_config = ConfigDict(frozen=True)
    mode: str
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    distance_meters: float = Field(ge=0, allow_inf_nan=False)
    guidance: str = ""


class AccessRoute(BaseModel):
    model_config = ConfigDict(frozen=True)
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    distance_meters: float = Field(ge=0, allow_inf_nan=False)
    transfers: int = Field(ge=0, strict=True)
    steps: tuple[AccessStep, ...] = Field(min_length=1)


class AccessLeg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    transport_modes: tuple[str, ...] = Field(min_length=1)
    duration_minutes: float = Field(ge=0, allow_inf_nan=False)
    distance_meters: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    departure_time: datetime
    provider: str = Field(min_length=1)
    note: str = ""
    transfers: int = Field(default=0, ge=0)
    steps: tuple[AccessStep, ...] = ()
    origin_point: AccessPoint | None = None
    destination_point: AccessPoint | None = None
    # True when Kakao publictraffic was unavailable and duration is distance-estimated.
    estimated: bool = False

    @model_validator(mode="after")
    def aware_departure(self) -> Self:
        if self.departure_time.tzinfo is None:
            raise ValueError("Access departure must include a timezone")
        return self

    @computed_field
    @property
    def arrival_time(self) -> datetime:
        return self.departure_time + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True)
class BoardingAssessment:
    access_leg: AccessLeg
    buffer_minutes: float
    transport_departure: datetime
    transport_arrival: datetime

    def __post_init__(self) -> None:
        import math
        if not math.isfinite(self.buffer_minutes) or self.buffer_minutes < 0:
            raise ValueError("Invalid boarding buffer")
        if self.transport_departure.tzinfo is None or self.transport_arrival.tzinfo is None:
            raise ValueError("Transport times must include a timezone")
        if self.transport_arrival <= self.transport_departure:
            raise ValueError("Arrival must follow departure")

    @property
    def ready_time(self) -> datetime:
        return self.access_leg.arrival_time + timedelta(minutes=self.buffer_minutes)

    @property
    def is_feasible(self) -> bool:
        return self.ready_time <= self.transport_departure

    @property
    def waiting_minutes(self) -> float:
        """Slack AFTER access and buffer; negative means the connection is missed."""
        return (self.transport_departure - self.ready_time).total_seconds() / 60

    @property
    def total_duration_minutes(self) -> float:
        """Origin departure to long-distance arrival, including all waiting once."""
        return (self.transport_arrival - self.access_leg.departure_time).total_seconds() / 60
