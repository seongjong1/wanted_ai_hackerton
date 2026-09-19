"""Shared route-segment display helpers for Access and Timeline Travel.

Presentation only — does not call Kakao / TAGO / Groq.
"""
from __future__ import annotations

from models.access import AccessStep

MODE_LABELS = {
    "BUS": "버스",
    "SUBWAY": "지하철",
    "WALKING": "도보",
    "TRAIN": "열차",
    "ESTIMATED": "추정",
    "SAME_PLACE": "같은 장소",
}


def meaningful_route_steps(steps: tuple[AccessStep, ...] | list[AccessStep] | None) -> list[AccessStep]:
    """Keep steps that carry real mode/time/distance/line/guidance — same filter as Access UI."""
    if not steps:
        return []
    out: list[AccessStep] = []
    for step in steps:
        mode = (getattr(step, "mode", "") or "").strip()
        if not mode or mode == "ESTIMATED":
            continue
        if (getattr(step, "duration_seconds", 0) > 0
                or getattr(step, "distance_meters", 0) > 0
                or (getattr(step, "guidance", "") or "").strip()
                or (getattr(step, "line_name", "") or "").strip()
                or (getattr(step, "start_name", "") or "").strip()):
            out.append(step)
    return out


def format_minutes(value: float) -> str:
    return f"{value:g}분" if float(value).is_integer() else f"약 {int(value + 0.5)}분"


def mode_label(mode: str) -> str:
    key = (mode or "").strip().upper()
    return MODE_LABELS.get(key, mode.strip() if mode else "이동")


def route_step_lines(step: AccessStep) -> list[str]:
    """User-facing lines for one segment — omit fields Kakao did not return."""
    seconds = float(getattr(step, "duration_seconds", 0) or 0)
    lines = [f"{mode_label(step.mode)} · {format_minutes(seconds / 60)}"]
    line_name = (getattr(step, "line_name", "") or "").strip()
    if line_name:
        lines.append(line_name)
    start = (getattr(step, "start_name", "") or "").strip()
    end = (getattr(step, "end_name", "") or "").strip()
    if start and end and start != end:
        lines.append(f"{start} → {end}")
    elif start and not end:
        lines.append(start)
    elif end and not start:
        lines.append(end)
    guidance = (getattr(step, "guidance", "") or "").strip()
    # Avoid repeating guidance when line/stops already cover the same info.
    if guidance and guidance not in lines and guidance != line_name:
        if not line_name and not (start or end):
            lines.append(guidance)
    return lines


def resolve_timeline_route_steps(
        event,
        *,
        selected=None,
        items=(),
        return_journey=None,
) -> list[AccessStep]:
    """Prefer event.route_steps, then AccessLeg / ScheduleItem sources.

    Cloud can keep a newer Timeline renderer with an older event payload that
    omitted route_steps even when Kakao already filled AccessLeg.steps.
    """
    found = meaningful_route_steps(getattr(event, "route_steps", ()) or ())
    if found:
        return found
    kind = getattr(event, "kind", "")
    detail = getattr(event, "detail", "") or ""
    if kind == "OUTBOUND" and "출발지 접근" in detail:
        leg = getattr(getattr(selected, "access", None), "access_leg", None)
        found = meaningful_route_steps(getattr(leg, "steps", ()) or ())
        if found:
            return found
    if kind == "TRAVEL":
        for item in items or ():
            if getattr(item, "item_type", "") != "TRAVEL":
                continue
            if (getattr(item, "start_datetime", None) == getattr(event, "start", None)
                    and getattr(item, "end_datetime", None) == getattr(event, "end", None)):
                found = meaningful_route_steps(getattr(item, "route_steps", ()) or ())
                if found:
                    return found
    if kind == "RETURN_HUB" and return_journey is not None:
        found = meaningful_route_steps(getattr(return_journey.to_hub, "steps", ()) or ())
        if found:
            return found
    if kind == "RETURN_ACCESS" and return_journey is not None:
        found = meaningful_route_steps(getattr(return_journey.to_origin, "steps", ()) or ())
        if found:
            return found
    return []
