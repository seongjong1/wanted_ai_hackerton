"""Structured replan events and results — Phase 6.1 (no NL / no LLM)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from models.access import AccessPoint
from models.place import PlaceCandidate
from models.schedule import ScheduleItem, TripSchedule
from models.transport import TransportCandidate
from models.trip_request import TripRequest


class ReplanEventType(str, Enum):
    PLACE_CLOSED = "PLACE_CLOSED"
    USER_SKIP = "USER_SKIP"
    DELAY = "DELAY"
    CHANGE_PREFERENCE = "CHANGE_PREFERENCE"
    CURRENT_LOCATION_CHANGED = "CURRENT_LOCATION_CHANGED"
    FATIGUE = "FATIGUE"
    MEAL_CHANGE = "MEAL_CHANGE"
    # Reserved for Phase 6.2+
    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    WEATHER = "WEATHER"
    USER_ADD_PLACE = "USER_ADD_PLACE"


class ItemProgress(str, Enum):
    COMPLETED = "COMPLETED"
    IN_PROGRESS = "IN_PROGRESS"
    FUTURE = "FUTURE"


class LocationStatus(str, Enum):
    KNOWN = "KNOWN"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class ReplanStatus(str, Enum):
    REPLAN_SUCCESS = "REPLAN_SUCCESS"
    REPLAN_PARTIAL = "REPLAN_PARTIAL"
    REPLAN_INFEASIBLE = "REPLAN_INFEASIBLE"
    REPLAN_FAILED = "REPLAN_FAILED"
    REPLAN_NO_CHANGE = "REPLAN_NO_CHANGE"


class ReplanEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_type: ReplanEventType
    occurred_at: datetime
    place_id: str | None = None
    delay_minutes: int | None = Field(default=None, ge=0)
    current_latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    current_longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    preference_changes: tuple[str, ...] = ()
    # Phase 6.2 meal direction: LUNCH | DINNER | FLEXIBLE | NONE | ""
    target_meal_role: str = ""
    source: Literal["USER_REPORTED", "SYSTEM", ""] = "USER_REPORTED"
    note: str = ""


class ReplanAnchor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    current_datetime: datetime
    current_location: AccessPoint | None = None
    current_day_index: int = Field(default=1, ge=1)
    location_status: LocationStatus = LocationStatus.UNKNOWN


class ReplanContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    trip: TripRequest
    original_schedule: TripSchedule
    selected_transport: TransportCandidate
    current_datetime: datetime
    event: ReplanEvent
    completed_item_ids: frozenset[str] = frozenset()
    current_location: AccessPoint | None = None
    place_pool: tuple[PlaceCandidate, ...] = ()


class ReplanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: ReplanStatus
    original_schedule: TripSchedule
    proposed_schedule: TripSchedule | None = None
    changed_items: tuple[ScheduleItem, ...] = ()
    removed_items: tuple[ScheduleItem, ...] = ()
    added_items: tuple[ScheduleItem, ...] = ()
    main_status: str = ""
    reasons: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()
    attempts: int = Field(default=0, ge=0)
    anchor: ReplanAnchor | None = None


def item_key(item: ScheduleItem) -> str:
    """Stable replan identity for a schedule leaf (place_id + start instant)."""
    return f"{item.place_id}|{item.start_datetime.isoformat()}"
