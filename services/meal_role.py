"""MAIN importance vs meal-slot role — Phase 4.7 flexible restaurant anchors."""
from __future__ import annotations

from enum import Enum

from models.place import PlaceCandidate
from models.trip_request import Preference


class MealRole(str, Enum):
    """When a MAIN place should occupy a meal window (not Place Importance)."""
    LUNCH = "LUNCH"
    DINNER = "DINNER"
    FLEXIBLE = "FLEXIBLE"
    NONE = "NONE"


# Maps MealRole → visit_slot meal window labels used in ScheduleSettings.meal_windows.
MEAL_ROLE_LABELS: dict[MealRole, tuple[str, ...]] = {
    MealRole.LUNCH: ("점심",),
    MealRole.DINNER: ("저녁",),
    MealRole.FLEXIBLE: ("점심", "저녁"),
    MealRole.NONE: (),
}


def is_restaurant(candidate: PlaceCandidate) -> bool:
    """True for meal places (음식점), excluding cafes treated as REST."""
    category = candidate.category or ""
    if any(term in category for term in ("카페", "커피전문점")):
        return False
    return "음식점" in category or Preference.FOOD in candidate.matched_preferences


def resolve_meal_role(candidate: PlaceCandidate | None,
                      override: MealRole | str | None = None) -> MealRole:
    """Default: restaurant MAIN → FLEXIBLE; tourism/etc → NONE.

    ``override`` supports future UI ('자동'/점심/저녁) without requiring it now.
    """
    if override is not None:
        if isinstance(override, MealRole):
            return override
        text = str(override).strip().upper()
        mapping = {
            "LUNCH": MealRole.LUNCH, "점심": MealRole.LUNCH,
            "DINNER": MealRole.DINNER, "저녁": MealRole.DINNER,
            "FLEXIBLE": MealRole.FLEXIBLE, "자동": MealRole.FLEXIBLE, "AUTO": MealRole.FLEXIBLE,
            "NONE": MealRole.NONE,
        }
        if text in mapping:
            return mapping[text]
    if candidate is None:
        return MealRole.NONE
    if is_restaurant(candidate):
        return MealRole.FLEXIBLE
    return MealRole.NONE


def meal_labels_for_role(role: MealRole) -> tuple[str, ...] | None:
    """None → all meal windows (legacy); empty → non-meal; else restrict labels."""
    if role == MealRole.NONE:
        return None
    return MEAL_ROLE_LABELS[role]
