"""Typed destination itinerary; arrival is a boundary, not a zero-length item."""
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from models.access import AccessPoint, AccessLeg, AccessStep
from models.transport import TransportCandidate


class ReturnStatus(str, Enum):
    """Return search is independent from outbound candidate presence."""
    RETURN_AVAILABLE = "RETURN_AVAILABLE"
    RETURN_INFEASIBLE = "RETURN_INFEASIBLE"
    RETURN_UNKNOWN = "RETURN_UNKNOWN"
    RETURN_NONE = "RETURN_NONE"


class ReturnJourney(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    to_hub: AccessLeg
    transport: TransportCandidate
    boarding_buffer_minutes: float = Field(ge=0)
    to_origin: AccessLeg


class ScheduleItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    item_type: Literal["TRAVEL", "PLACE", "MEAL"]
    place_id: str
    place_name: str
    start_datetime: datetime
    end_datetime: datetime
    origin: AccessPoint | None = None
    destination: AccessPoint | None = None
    travel_mode: tuple[str, ...] = ()
    travel_duration_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Kakao AccessRoute.steps preserved for Timeline detail (optional presentation).
    route_steps: tuple[AccessStep, ...] = ()
    estimated_duration: bool = False
    preference: str = ""
    meal_slot: str | None = None
    reason: str = ""
    open_status: Literal["UNKNOWN"] = "UNKNOWN"
    checks: tuple[str, ...] = ()
    validation_status: Literal["UNVALIDATED", "VALIDATED"] = "UNVALIDATED"

    @model_validator(mode="after")
    def positive_interval(self) -> Self:
        if self.start_datetime.tzinfo is None or self.end_datetime.tzinfo is None:
            raise ValueError("Timezone required")
        if self.end_datetime <= self.start_datetime:
            raise ValueError("Non-positive schedule interval")
        return self

    @computed_field
    @property
    def duration_minutes(self) -> float:
        return (self.end_datetime - self.start_datetime).total_seconds() / 60


class TripDaySchedule(BaseModel):
    """One calendar day inside a multi-day trip; ScheduleItem stays the leaf unit."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    day_index: int = Field(ge=1)
    date: date
    role: Literal["FIRST", "MIDDLE", "FINAL"]
    start_location: AccessPoint
    end_location: AccessPoint
    activity_start: datetime
    activity_end: datetime
    items: tuple[ScheduleItem, ...]

    @model_validator(mode="after")
    def valid_day(self) -> Self:
        if self.activity_start.tzinfo is None or self.activity_end.tzinfo is None:
            raise ValueError("Timezone required")
        if self.activity_end < self.activity_start:
            raise ValueError("Invalid day activity bounds")
        if self.date != self.activity_start.date():
            raise ValueError("Day date mismatch")
        return self


class TripSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    trip_start_datetime: datetime
    trip_end_datetime: datetime
    arrival_point: AccessPoint
    items: tuple[ScheduleItem, ...]
    days: tuple[TripDaySchedule, ...] = ()
    accommodation: AccessPoint | None = None
    # CONFIRMED = user lodging; PROVISIONAL = overnight base is destination hub (lodging undecided).
    accommodation_status: Literal["", "CONFIRMED", "PROVISIONAL"] = ""
    # Future: per-night lodging points. Empty ⇒ single ``accommodation`` applies to every overnight.
    accommodation_nights: tuple[AccessPoint, ...] = ()
    return_journey: ReturnJourney | None = None
    return_transport_candidates: tuple[TransportCandidate, ...] = ()
    return_status: ReturnStatus = ReturnStatus.RETURN_NONE
    final_arrival_datetime: datetime | None = None
    destination_activity_cutoff: datetime | None = None
    user_selected_place_id: str | None = None
    # INCLUDED | INFEASIBLE | "" — MAIN anchor placement status (Phase 4.7).
    anchor_status: str = ""
    # LUNCH | DINNER | FLEXIBLE | NONE | "" — meal-slot role chosen for MAIN (not Place Importance).
    anchor_meal_role: str = ""
    validation_status: Literal["UNVALIDATED", "VALIDATED"] = "UNVALIDATED"

    @model_validator(mode="after")
    def valid_bounds(self) -> Self:
        if self.trip_start_datetime.tzinfo is None or self.trip_end_datetime.tzinfo is None:
            raise ValueError("Timezone required")
        if self.trip_end_datetime <= self.trip_start_datetime:
            raise ValueError("Invalid schedule bounds")
        return self

    @computed_field
    @property
    def total_travel_minutes(self) -> float:
        return sum(i.duration_minutes for i in self.items if i.item_type == "TRAVEL")

    @computed_field
    @property
    def total_activity_minutes(self) -> float:
        return sum(i.duration_minutes for i in self.items if i.item_type != "TRAVEL")


@dataclass
class ScheduleResult:
    status: str
    schedule: TripSchedule | None = None
    notices: list[str] = field(default_factory=list)
    ai_status: str = "미사용"
