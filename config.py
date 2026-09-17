"""Configuration independent of Streamlit; never include secrets in repr/logs."""
import os
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScheduleSettings:
    meal_minutes: int = 60
    sightseeing_minutes: int = 60
    shopping_minutes: int = 60
    experience_minutes: int = 90
    rest_minutes: int = 30
    default_minutes: int = 60
    # Micro-landmark / pass-through stays (not full sightseeing blocks).
    short_stop_minutes: int = 15
    pass_through_minutes: int = 5
    supporting_sightseeing_minutes: int = 40
    min_schedule_suitability: int = 40
    min_gap_fill_suitability: int = 45
    # Cafe soft-cap when user did not select REST as a preference (penalty-based, not hard ban).
    cafe_soft_cap: int = 1
    cafe_max_without_rest_pref: int = 2
    consecutive_category_penalty: int = 35
    cafe_repeat_penalty: int = 45
    candidate_limit: int = 15
    shortlist_limit: int = 3
    max_visits: int = 8
    max_schedule_days: int = 8
    max_route_calls: int = 32
    max_schedule_attempts: int = 2
    max_distance_meters: int = 20000
    day_start_hour: int = 9
    day_end_hour: int = 20
    night_start_hour: int = 18
    min_supporting_activity_gap_minutes: int = 45
    supporting_search_limit: int = 12
    max_gap_fill_iterations: int = 8
    # Gaps at/above this size always run support search (quality permitting).
    long_gap_minutes: int = 90
    very_long_gap_minutes: int = 120
    gap_safety_buffer_minutes: int = 5
    late_lunch_end_hour: int = 16
    # Middle-day activity window defaults (final-day home return uses TripRequest.end_time).
    default_daily_start_hour: int = 9
    default_daily_end_hour: int = 21
    # When a day starts and ends at the same accommodation, reserve this travel buffer.
    accommodation_return_buffer_minutes: int = 40
    # Phase 4.8 lodging recommendations (Kakao Local only; no hallucinated ratings/prices).
    accommodation_search_queries: tuple[str, ...] = ("호텔", "숙박", "모텔")
    accommodation_search_pages: int = 1
    accommodation_search_size: int = 15
    accommodation_prefilter_limit: int = 5
    accommodation_route_eval_limit: int = 5
    accommodation_recommend_limit: int = 3
    meal_windows: tuple[tuple[str, int, int], ...] = (("점심", 11, 15), ("저녁", 17, 20))

    def __post_init__(self):
        numbers = (self.meal_minutes, self.sightseeing_minutes, self.shopping_minutes,
                   self.experience_minutes, self.rest_minutes, self.default_minutes,
                   self.short_stop_minutes, self.pass_through_minutes,
                   self.supporting_sightseeing_minutes,
                   self.min_schedule_suitability, self.min_gap_fill_suitability,
                   self.cafe_soft_cap, self.cafe_max_without_rest_pref,
                   self.consecutive_category_penalty, self.cafe_repeat_penalty,
                   self.candidate_limit, self.shortlist_limit, self.max_visits, self.max_schedule_days,
                   self.max_route_calls, self.max_schedule_attempts, self.max_distance_meters,
                   self.min_supporting_activity_gap_minutes, self.supporting_search_limit,
                   self.max_gap_fill_iterations, self.long_gap_minutes, self.very_long_gap_minutes,
                   self.gap_safety_buffer_minutes,
                   self.accommodation_return_buffer_minutes,
                   self.accommodation_search_pages, self.accommodation_search_size,
                   self.accommodation_prefilter_limit, self.accommodation_route_eval_limit,
                   self.accommodation_recommend_limit)
        if any(v <= 0 for v in numbers) or self.max_schedule_attempts > 3:
            raise ValueError("Invalid schedule limits")
        if not 0 <= self.day_start_hour < self.day_end_hour <= 24 or not 0 <= self.night_start_hour < 24:
            raise ValueError("Invalid schedule hours")
        if not 0 <= self.default_daily_start_hour < self.default_daily_end_hour <= 24:
            raise ValueError("Invalid default daily hours")
        if any(not 0 <= start < end <= 24 for _, start, end in self.meal_windows):
            raise ValueError("Invalid meal windows")
        if not 15 <= self.late_lunch_end_hour <= 17:
            raise ValueError("Invalid late lunch limit")
        if self.min_schedule_suitability > 100 or self.min_gap_fill_suitability > 100:
            raise ValueError("Invalid suitability thresholds")
        if not self.accommodation_search_queries:
            raise ValueError("Accommodation search queries required")
        if not (1 <= self.accommodation_recommend_limit
                <= self.accommodation_route_eval_limit
                <= self.accommodation_prefilter_limit <= 15):
            raise ValueError("Invalid accommodation recommend limits")
        if not 1 <= self.accommodation_search_pages <= 3 or not 1 <= self.accommodation_search_size <= 15:
            raise ValueError("Invalid accommodation search pagination")


@dataclass(frozen=True)
class Settings:
    data_go_kr_api_key: str = field(default="", repr=False)
    kakao_rest_api_key: str = field(default="", repr=False)
    groq_api_key: str = field(default="", repr=False)
    groq_model: str = "openai/gpt-oss-120b"
    enable_api_diagnostics: bool = False
    extended_activity_radius_meters: int = 2000
    local_origin_hub_scan_limit: int = 12
    local_origin_hub_limit: int = 3
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)


def load_settings(secrets: Mapping[str, object] | None = None,
                  environ: Mapping[str, str] | None = None) -> Settings:
    """Environment overrides nonempty Streamlit secrets; no key is required for UI."""
    source = secrets if secrets is not None else {}
    env = os.environ if environ is None else environ

    def read(name: str) -> str:
        value = env.get(name) or source.get(name, "")
        return str(value).strip()

    try:
        extended_radius = int(read("EXTENDED_ACTIVITY_RADIUS_METERS") or "2000")
        if not 501 <= extended_radius <= 20000:
            raise ValueError("out of range")
    except ValueError:
        logging.getLogger("travel_ai.config").warning("Invalid extended radius; defaulting to 2000m")
        extended_radius = 2000
    def limit(name: str, default: int, maximum: int) -> int:
        try:
            value = int(read(name) or str(default))
            if 1 <= value <= maximum:
                return value
        except ValueError:
            pass
        logging.getLogger("travel_ai.config").warning("Invalid %s; using default", name)
        return default

    return Settings(
        data_go_kr_api_key=read("DATA_GO_KR_API_KEY"),
        kakao_rest_api_key=read("KAKAO_REST_API_KEY"),
        groq_api_key=read("GROQ_API_KEY"),
        enable_api_diagnostics=read("ENABLE_API_DIAGNOSTICS").lower() in {"true", "1"},
        extended_activity_radius_meters=extended_radius,
        local_origin_hub_scan_limit=limit("LOCAL_ORIGIN_HUB_SCAN_LIMIT", 12, 30),
        local_origin_hub_limit=limit("LOCAL_ORIGIN_HUB_LIMIT", 3, 3),
        schedule=ScheduleSettings(
            max_gap_fill_iterations=limit("MAX_GAP_FILL_ITERATIONS", 8, 10),
            gap_safety_buffer_minutes=limit("GAP_SAFETY_BUFFER_MINUTES", 5, 30),
            min_supporting_activity_gap_minutes=limit("MIN_SUPPORTING_ACTIVITY_GAP_MINUTES", 45, 240),
            late_lunch_end_hour=max(15, min(17, limit("LATE_LUNCH_END_HOUR", 16, 17))),
            meal_minutes=limit("SCHEDULE_MEAL_MINUTES", 60, 240),
            sightseeing_minutes=limit("SCHEDULE_SIGHTSEEING_MINUTES", 60, 240),
            shopping_minutes=limit("SCHEDULE_SHOPPING_MINUTES", 60, 240),
            experience_minutes=limit("SCHEDULE_EXPERIENCE_MINUTES", 90, 480),
            rest_minutes=limit("SCHEDULE_REST_MINUTES", 30, 240),
            default_minutes=limit("SCHEDULE_DEFAULT_MINUTES", 60, 240),
            short_stop_minutes=limit("SCHEDULE_SHORT_STOP_MINUTES", 15, 30),
            supporting_sightseeing_minutes=limit("SCHEDULE_SUPPORTING_SIGHTSEEING_MINUTES", 40, 90),
            min_schedule_suitability=limit("MIN_SCHEDULE_SUITABILITY", 40, 100),
            min_gap_fill_suitability=limit("MIN_GAP_FILL_SUITABILITY", 45, 100),
            max_route_calls=limit("MAX_SCHEDULE_ROUTE_CALLS", 32, 48),
            max_schedule_attempts=limit("MAX_SCHEDULE_ATTEMPTS", 2, 3),
            accommodation_prefilter_limit=limit("ACCOMMODATION_PREFILTER_LIMIT", 5, 15),
            accommodation_route_eval_limit=limit("ACCOMMODATION_ROUTE_EVAL_LIMIT", 5, 10),
            accommodation_recommend_limit=limit("ACCOMMODATION_RECOMMEND_LIMIT", 3, 5),
        ),
    )
