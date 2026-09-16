"""Schedule visit-worthiness separate from Kakao category / discovery ranking."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import ScheduleSettings
from models.place import PlaceCandidate
from models.trip_request import Preference, TripRequest
from providers.http_client import ProviderError

logger = logging.getLogger("travel_ai.suitability")


class VisitRole(str, Enum):
    PRIMARY_DESTINATION = "PRIMARY_DESTINATION"
    SUPPORTING_DESTINATION = "SUPPORTING_DESTINATION"
    SHORT_STOP = "SHORT_STOP"
    PASS_THROUGH = "PASS_THROUGH"
    UNSUITABLE = "UNSUITABLE"


# Category leaf / path signals for roadside micro-landmarks (not place-name blacklists).
_MICRO_LANDMARK_CATEGORY = (
    "탑,비석", "기념비", "기념탑", "비석", "동상", "조형물", "표지석", "기념석",
)
_MICRO_LANDMARK_NAME_SUFFIX = (
    "기념비", "기념탑", "비석", "동상", "조형물", "표지석", "기념석", "기념상",
)
_PRIMARY_CATEGORY = (
    "박물관", "미술관", "전시관", "과학관", "테마파크", "동물원", "수족관",
    "시장", "테마거리", "온천", "해수욕장", "케이블카", "스키", "놀이동산",
    "궁궐", "성곽", "왕릉", "사찰", "수목원",
    "체험", "공방", "산악",
)
_PRIMARY_NAME = (
    "박물관", "미술관", "전시관", "테마파크", "시장", "온천", "해수욕장",
    "국립공원", "도립공원", "수목원", "케이블카",
)
_SUPPORTING_CATEGORY = ("카페", "커피", "공원", "산책", "하천", "문화의집", "도서관")
_SUPPORTING_NAME = ("카페", "커피", "공원", "산책로")
_PASS_THROUGH_CATEGORY = ("주차장", "화장실", "편의점", "버스정류장", "지하철역", "터미널")
_UNSUITABLE_CATEGORY = ("병원", "약국", "부동산", "중개", "학원", "은행", "관공서", "경찰")
# Strong upgrade beyond generic sightseeing preference (heritage / venue signals).
_UPGRADE_SIGNALS = (
    "국보", "보물", "사적", "세계유산", "유네스코", "박물관", "미술관", "전시",
    "궁궐", "산성", "읍성", "왕릉", "사찰", "대성전",
)


@dataclass(frozen=True)
class ItineraryAssessment:
    visit_role: VisitRole
    suitability_score: int
    duration_minutes: int
    source: Literal["rules", "groq"] = "rules"


class RoleChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_id: str = Field(min_length=1)
    role: Literal["PRIMARY", "SUPPORTING", "SHORT_STOP", "LOW_PRIORITY"]


class RoleBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choices: list[RoleChoice] = Field(max_length=20)


def _leaf_category(category: str) -> str:
    parts = [p.strip() for p in category.split(">") if p.strip()]
    return parts[-1] if parts else ""


def is_micro_landmark(candidate: PlaceCandidate) -> bool:
    category = candidate.category or ""
    leaf = _leaf_category(category)
    if any(term == leaf or term in leaf for term in _MICRO_LANDMARK_CATEGORY):
        return True
    if "탑,비석" in category.replace(" ", ""):
        return True
    name = candidate.place_name or ""
    if any(name.endswith(suffix) for suffix in _MICRO_LANDMARK_NAME_SUFFIX):
        return True
    # Compact memorial towers often named *의탑 / *기념탑 without a matching leaf.
    if name.endswith("의탑") or name.endswith("기념탑"):
        return True
    return False


def _has_upgrade_signal(candidate: PlaceCandidate) -> bool:
    blob = f"{candidate.place_name} {candidate.category}"
    return any(signal in blob for signal in _UPGRADE_SIGNALS)


def classify_visit_role(candidate: PlaceCandidate, trip: TripRequest | None = None) -> VisitRole:
    """Deterministic itinerary role from category/name signals (never place-id blacklists)."""
    category = candidate.category or ""
    name = candidate.place_name or ""
    blob = f"{name} {category}"

    if any(term in category for term in _UNSUITABLE_CATEGORY):
        return VisitRole.UNSUITABLE
    if any(term in category for term in _PASS_THROUGH_CATEGORY):
        return VisitRole.PASS_THROUGH

    micro = is_micro_landmark(candidate)
    if micro:
        # Pure roadside monuments stay SHORT_STOP unless a strong heritage/venue signal exists.
        if _has_upgrade_signal(candidate):
            return VisitRole.SUPPORTING_DESTINATION
        return VisitRole.SHORT_STOP

    if any(term in category for term in _PRIMARY_CATEGORY) or any(term in name for term in _PRIMARY_NAME):
        return VisitRole.PRIMARY_DESTINATION
    if any(term in category for term in ("음식점", "한식", "중식", "일식", "양식", "분식", "육류")):
        return VisitRole.PRIMARY_DESTINATION
    if any(term in category for term in _SUPPORTING_CATEGORY) or any(term in name for term in _SUPPORTING_NAME):
        return VisitRole.SUPPORTING_DESTINATION
    if "관광" in category or "명소" in category or "문화" in category:
        # Generic tourism (non-micro) can be a full stop when sightseeing was matched at discovery.
        if candidate.matched_preferences and Preference.SIGHTSEEING in candidate.matched_preferences:
            return VisitRole.PRIMARY_DESTINATION
        return VisitRole.SUPPORTING_DESTINATION
    return VisitRole.SUPPORTING_DESTINATION


def suitability_score(candidate: PlaceCandidate, trip: TripRequest,
                      role: VisitRole | None = None) -> int:
    """0–100 schedule worthiness; independent from discovery preference_score."""
    role = role or classify_visit_role(candidate, trip)
    base = {
        VisitRole.PRIMARY_DESTINATION: 75,
        VisitRole.SUPPORTING_DESTINATION: 55,
        VisitRole.SHORT_STOP: 20,
        VisitRole.PASS_THROUGH: 5,
        VisitRole.UNSUITABLE: 0,
    }[role]
    prefs = set(trip.preferences)
    overlap = len(set(candidate.matched_preferences) & prefs)
    base += 8 * min(overlap, 2)
    if candidate.matched_queries:
        base += 3 * min(len(candidate.matched_queries), 3)
    if role == VisitRole.SHORT_STOP and not _has_upgrade_signal(candidate):
        base = min(base, 25)
    if is_micro_landmark(candidate) and role == VisitRole.SHORT_STOP:
        base = min(base, 22)
    # Generic shallow "관광,명소" without richer subtype stays modest.
    leaf = _leaf_category(candidate.category)
    if leaf in {"관광,명소", "명소"} and role != VisitRole.PRIMARY_DESTINATION:
        base = min(base, 50)
    return max(0, min(100, base))


def duration_for_role(role: VisitRole, preference: Preference, config: ScheduleSettings,
                      *, meal: bool = False) -> int:
    if meal:
        return config.meal_minutes
    if role == VisitRole.SHORT_STOP:
        return config.short_stop_minutes
    if role == VisitRole.PASS_THROUGH:
        return config.pass_through_minutes
    if role == VisitRole.UNSUITABLE:
        return 0
    if role == VisitRole.SUPPORTING_DESTINATION:
        if preference == Preference.REST:
            return config.rest_minutes
        if preference == Preference.SIGHTSEEING:
            return min(config.sightseeing_minutes, config.supporting_sightseeing_minutes)
        return config.rest_minutes
    # PRIMARY
    return {
        Preference.SIGHTSEEING: config.sightseeing_minutes,
        Preference.SHOPPING: config.shopping_minutes,
        Preference.EXPERIENCE: config.experience_minutes,
        Preference.REST: config.rest_minutes,
    }.get(preference, config.default_minutes)


def assess_place(candidate: PlaceCandidate, trip: TripRequest, preference: Preference,
                 config: ScheduleSettings, *, meal: bool = False,
                 role_override: VisitRole | None = None) -> ItineraryAssessment:
    role = role_override or classify_visit_role(candidate, trip)
    score = suitability_score(candidate, trip, role)
    minutes = duration_for_role(role, preference, config, meal=meal)
    return ItineraryAssessment(visit_role=role, suitability_score=score, duration_minutes=minutes)


def schedule_eligible(assessment: ItineraryAssessment, config: ScheduleSettings, *,
                      for_gap_fill: bool = False, preferred: bool = False) -> bool:
    if preferred:
        return assessment.visit_role != VisitRole.UNSUITABLE and assessment.duration_minutes > 0
    if assessment.visit_role in (VisitRole.UNSUITABLE, VisitRole.PASS_THROUGH):
        return False
    if for_gap_fill:
        # Quality over zero-gap: never pad long free time with micro-landmarks.
        if assessment.visit_role == VisitRole.SHORT_STOP:
            return False
        return assessment.suitability_score >= config.min_gap_fill_suitability
    if assessment.visit_role == VisitRole.SHORT_STOP:
        return False
    return assessment.suitability_score >= config.min_schedule_suitability


def activity_bucket(candidate: PlaceCandidate) -> str:
    """Coarse schedule category for diversity (MEAL/CAFE/TOURISM/…)."""
    category = candidate.category or ""
    if ("음식점" in category or Preference.FOOD in candidate.matched_preferences) and not any(
            term in category for term in ("카페", "커피전문점")):
        return "MEAL"
    if any(term in category for term in ("카페", "커피")):
        return "CAFE"
    if any(term in category for term in ("시장",)):
        return "MARKET"
    if any(term in category for term in ("공원", "산책", "하천", "수목원")):
        return "WALK"
    if any(term in category for term in ("박물관", "미술관", "전시", "문화")):
        return "CULTURE"
    if any(term in category for term in ("체험", "공방")):
        return "EXPERIENCE"
    if any(term in category for term in ("관광", "명소", "전망", "테마거리")):
        return "TOURISM"
    return "REST"


def visited_buckets(items, known: dict[str, PlaceCandidate]) -> list[str]:
    buckets = []
    for item in items:
        if item.item_type == "TRAVEL":
            continue
        candidate = known.get(item.place_id)
        if candidate is not None:
            buckets.append(activity_bucket(candidate))
        elif item.item_type == "MEAL":
            buckets.append("MEAL")
        elif item.preference == Preference.REST.value:
            buckets.append("CAFE")
        else:
            buckets.append("TOURISM")
    return buckets


def prefers_cafe(trip: TripRequest) -> bool:
    return Preference.REST in trip.preferences


def diversity_penalty(candidate: PlaceCandidate, trip: TripRequest, prior_buckets: list[str],
                      config: ScheduleSettings) -> float:
    """Score deduction for category imbalance; higher = worse. Soft caps, not hard bans."""
    bucket = activity_bucket(candidate)
    penalty = 0.0
    cafe_pref = prefers_cafe(trip)
    cafe_count = sum(1 for b in prior_buckets if b in {"CAFE", "REST"})
    if prior_buckets and prior_buckets[-1] == bucket and bucket != "MEAL":
        # Consecutive same non-meal category (esp. CAFE→CAFE).
        penalty += config.consecutive_category_penalty
        if bucket in {"CAFE", "REST"} and not cafe_pref:
            penalty += config.cafe_repeat_penalty
    if bucket in {"CAFE", "REST"} and not cafe_pref:
        if cafe_count >= config.cafe_soft_cap:
            penalty += config.cafe_repeat_penalty
        if cafe_count >= config.cafe_max_without_rest_pref:
            penalty += config.cafe_repeat_penalty * 2
    # Reward under-represented enrichment categories when filling after meals.
    enrichment = {"TOURISM", "CULTURE", "WALK", "MARKET", "EXPERIENCE"}
    if bucket in enrichment and not any(b in enrichment for b in prior_buckets):
        penalty -= 15
    if bucket in enrichment and cafe_count >= 1:
        penalty -= 10
    return penalty


def cafe_imbalanced(prior_buckets: list[str], trip: TripRequest, config: ScheduleSettings) -> bool:
    cafes = sum(1 for b in prior_buckets if b in {"CAFE", "REST"})
    enrichment = sum(1 for b in prior_buckets if b in {"TOURISM", "CULTURE", "WALK", "MARKET", "EXPERIENCE"})
    if prefers_cafe(trip):
        return cafes >= 3 and enrichment == 0
    return cafes >= 2 and enrichment == 0


def allow_cafe_candidate(candidate: PlaceCandidate, trip: TripRequest, prior_buckets: list[str],
                         config: ScheduleSettings, *, alternatives_exist: bool,
                         for_gap_fill: bool = False) -> bool:
    """Limit cafe/rest stacking when the user did not choose 휴식; prefer free time over cafe spam."""
    bucket = activity_bucket(candidate)
    rest_like = bucket in {"CAFE", "REST"}
    if not rest_like:
        return True
    if prefers_cafe(trip):
        return True
    rest_count = sum(1 for b in prior_buckets if b in {"CAFE", "REST"})
    consecutive = 0
    for prior in reversed(prior_buckets):
        if prior in {"CAFE", "REST"}:
            consecutive += 1
        else:
            break
    # Gap fill: after the soft cap, keep free time rather than another cafe.
    if for_gap_fill and rest_count >= config.cafe_soft_cap:
        return False
    if consecutive >= 1 and for_gap_fill:
        return False
    if consecutive >= 2 and alternatives_exist:
        return False
    if rest_count >= config.cafe_max_without_rest_pref and alternatives_exist:
        return False
    return True


def _groq_role_to_visit(label: str) -> VisitRole:
    return {
        "PRIMARY": VisitRole.PRIMARY_DESTINATION,
        "SUPPORTING": VisitRole.SUPPORTING_DESTINATION,
        "SHORT_STOP": VisitRole.SHORT_STOP,
        "LOW_PRIORITY": VisitRole.PASS_THROUGH,
    }.get(label, VisitRole.SHORT_STOP)


def refine_roles_with_groq(groq, candidates: list[PlaceCandidate], trip: TripRequest,
                           deterministic: dict[str, VisitRole]) -> dict[str, VisitRole]:
    """Optional assist: classify supplied candidates only; never invent places or facts."""
    if groq is None or not candidates:
        return dict(deterministic)
    payload = {
        "preferences": [p.value for p in trip.preferences],
        "candidates": [
            {"place_id": c.place_id, "place_name": c.place_name, "category": c.category,
             "address": c.address,
             "rule_role": deterministic.get(c.place_id, VisitRole.SUPPORTING_DESTINATION).value}
            for c in candidates[:15]
        ],
    }
    try:
        batch = groq.generate_structured(
            "Classify each supplied candidate for itinerary time worthiness only. "
            "Use PRIMARY, SUPPORTING, SHORT_STOP, or LOW_PRIORITY. "
            "Do not invent places, ratings, reviews, hours, or historical claims. "
            "Micro monuments (탑/비석/기념비/동상/조형물) are SHORT_STOP unless category clearly "
            "indicates a major museum/palace/park venue.",
            payload, RoleBatch)
        if not isinstance(batch, RoleBatch):
            raise ValueError("unexpected suitability schema")
    except (ProviderError, ValidationError, ValueError, TypeError, AttributeError):
        logger.warning("suitability_groq outcome=fallback")
        return dict(deterministic)
    known = {c.place_id for c in candidates}
    out = dict(deterministic)
    for choice in batch.choices:
        if choice.place_id not in known:
            continue
        proposed = _groq_role_to_visit(choice.role)
        current = out.get(choice.place_id, VisitRole.SUPPORTING_DESTINATION)
        # Groq may demote freely; promote micro-landmarks only to SUPPORTING, never jump to PRIMARY.
        if is_micro_landmark(next(c for c in candidates if c.place_id == choice.place_id)):
            if proposed == VisitRole.PRIMARY_DESTINATION:
                proposed = VisitRole.SUPPORTING_DESTINATION
            if proposed in (VisitRole.SHORT_STOP, VisitRole.PASS_THROUGH, VisitRole.SUPPORTING_DESTINATION):
                out[choice.place_id] = proposed
            continue
        # Demotion always allowed; promotion only one step from deterministic SHORT_STOP.
        rank = [VisitRole.UNSUITABLE, VisitRole.PASS_THROUGH, VisitRole.SHORT_STOP,
                VisitRole.SUPPORTING_DESTINATION, VisitRole.PRIMARY_DESTINATION]
        if rank.index(proposed) <= rank.index(current) + 1:
            out[choice.place_id] = proposed
    return out
