"""Resolved travel-area scope — separate from Transport hub resolution."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResolutionType(str, Enum):
    ADMIN_REGION = "ADMIN_REGION"
    MULTI_ADMIN_REGION = "MULTI_ADMIN_REGION"
    AREA_CENTER = "AREA_CENTER"
    DIRECT_PLACE = "DIRECT_PLACE"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"


class AdminScope(BaseModel):
    """Normalized administrative tokens (suffixes already stripped via region_name)."""
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    province: str = Field(min_length=1)
    city: str = Field(min_length=1)
    district: str = ""


class ResolvedDestination(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    original_query: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    normalized_query: str = Field(min_length=1)
    resolution_type: ResolutionType = ResolutionType.FAILED
    center_latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    center_longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    administrative_scopes: tuple[AdminScope, ...] = ()
    radius_m: int = Field(default=0, ge=0)
    status: Literal["RESOLVED", "FAILED", "AMBIGUOUS"] = "FAILED"
    source: str = ""
    notice: str = ""

    @property
    def is_resolved(self) -> bool:
        return self.status == "RESOLVED"
