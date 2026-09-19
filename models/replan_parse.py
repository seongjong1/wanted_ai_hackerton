"""Phase 6.2 — NL parse results (intent only; no schedule generation)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from models.replan import ReplanEvent, ReplanEventType, ReplanResult


class ParseSource(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    GROQ = "GROQ"
    FALLBACK = "FALLBACK"


class ParsedReplanIntent(BaseModel):
    """Intermediate intent — place_id / coords must be filled by deterministic resolvers."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_type: ReplanEventType | Literal["UNSUPPORTED"] = "UNSUPPORTED"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    target_place_text: str | None = None
    target_place_id: str | None = None  # only from schedule resolution, never LLM invention
    delay_minutes: int | None = Field(default=None, ge=0)
    current_location_text: str | None = None
    current_latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    current_longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    preference_changes: tuple[str, ...] = ()
    target_meal_role: str = ""  # LUNCH | DINNER | FLEXIBLE | NONE | ""
    source: Literal["USER_REPORTED", "SYSTEM", ""] = "USER_REPORTED"
    requires_confirmation: bool = False
    ambiguity_reason: str = ""
    supported: bool = True
    reason: str = ""
    parse_source: ParseSource = ParseSource.DETERMINISTIC


class GroqIntentItem(BaseModel):
    """Schema passed to Groq — no place_id / no coordinates allowed from the model."""
    model_config = ConfigDict(extra="ignore")
    event_type: str = "UNSUPPORTED"
    confidence: float = 0.0
    delay_minutes: int | None = None
    target_place_text: str | None = None
    target_meal_role: str | None = None
    preference_changes: list[str] = Field(default_factory=list)
    current_location_text: str | None = None
    reason: str = ""
    requires_confirmation: bool = False
    ambiguity_reason: str = ""


class GroqIntentBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    intents: list[GroqIntentItem] = Field(default_factory=list)


class ParseReplanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_text: str
    intents: tuple[ParsedReplanIntent, ...] = ()
    events: tuple[ReplanEvent, ...] = ()
    requires_confirmation: bool = False
    notices: tuple[str, ...] = ()
    parse_source: ParseSource = ParseSource.DETERMINISTIC
    occurred_at: datetime | None = None


class NlReplanApplyResult(BaseModel):
    """Parser → sequential Phase 6.1 engine application."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    parse: ParseReplanResult
    replan: ReplanResult | None = None
    applied_events: tuple[ReplanEvent, ...] = ()
    skipped: bool = False
    notices: tuple[str, ...] = ()
