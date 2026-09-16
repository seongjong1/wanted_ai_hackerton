from datetime import date, time
from enum import Enum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Preference(str, Enum):
    FOOD = "맛집"
    SIGHTSEEING = "관광"
    SHOPPING = "쇼핑"
    EXPERIENCE = "체험"
    REST = "휴식"
    NIGHT_VIEW = "야경"
    DRINK = "술"
    DATE = "데이트"
    FAMILY = "가족 여행"
    SOLO = "혼자 여행"


RADIUS_OPTIONS = ("100m", "200m", "300m", "400m", "500m", "500m 이상")


class TripRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", frozen=True)

    departure: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    start_date: date
    end_date: date
    departure_time: time
    end_time: time
    has_accommodation: bool
    allergies: tuple[str, ...] = ()
    has_pet: bool
    has_child: bool
    # Open-ended option is explicit, never silently converted into a search radius.
    activity_radius: Literal["100m", "200m", "300m", "400m", "500m", "500m 이상"]
    preferences: tuple[Preference, ...] = Field(min_length=1)

    @field_validator("allergies")
    @classmethod
    def clean_allergies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(v.strip() for v in values if v.strip()))
        if len(cleaned) > 30 or any(len(v) > 100 for v in cleaned):
            raise ValueError("알레르기는 항목당 100자, 최대 30개까지 입력하세요.")
        return cleaned

    @model_validator(mode="after")
    def validate_trip(self) -> Self:
        normalize = lambda text: "".join(text.split()).casefold()
        if normalize(self.departure) == normalize(self.destination):
            raise ValueError("출발지와 여행지는 서로 달라야 합니다.")
        if self.end_date < self.start_date:
            raise ValueError("종료 날짜는 시작 날짜보다 빠를 수 없습니다.")
        if self.departure_time.tzinfo or self.end_time.tzinfo:
            raise ValueError("시간은 한국 현지 시각으로 입력하세요.")
        if self.start_date == self.end_date and self.end_time <= self.departure_time:
            raise ValueError("당일 여행 종료시간은 출발시간보다 늦어야 합니다.")
        return self
