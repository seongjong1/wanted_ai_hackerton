"""Parse Kakao publictraffic step payloads into AccessStep — no invented fields."""
from __future__ import annotations

from models.access import AccessStep

_KNOWN_MODES = frozenset({"WALKING", "BUS", "SUBWAY", "TRAIN", "ESTIMATED", "SAME_PLACE"})


def parse_access_step(raw_step: object) -> AccessStep | None:
    """Build AccessStep from one Kakao route step; skip malformed rows."""
    if not isinstance(raw_step, dict):
        return None
    props = raw_step.get("properties")
    if not isinstance(props, dict):
        return None
    try:
        duration = float(props["time"])
        distance = float(props["distance"])
    except (KeyError, TypeError, ValueError):
        return None
    if duration < 0 or distance < 0:
        return None

    raw_type = str(props.get("type") or "").strip()
    vehicles = props.get("vehicles") if isinstance(props.get("vehicles"), list) else []
    stops = props.get("stops") if isinstance(props.get("stops"), list) else []

    line_name = ""
    vehicle_mode = ""
    for vehicle in vehicles:
        if not isinstance(vehicle, dict):
            continue
        name = str(vehicle.get("name") or "").strip()
        if name and not line_name:
            line_name = name
        vtype = str(vehicle.get("type") or "").strip().upper()
        if vtype in {"BUS", "SUBWAY"} and not vehicle_mode:
            vehicle_mode = vtype

    raw_upper = raw_type.upper()
    if raw_upper in _KNOWN_MODES:
        mode = raw_upper
    elif vehicle_mode:
        mode = vehicle_mode
    elif vehicles:
        # Vehicle present but type is a bus subtype label (e.g. 마을) — mode is bus.
        mode = "BUS"
    else:
        mode = raw_type or "WALKING"

    start_name = ""
    end_name = ""
    stop_names = []
    for stop in stops:
        if not isinstance(stop, dict):
            continue
        name = str(stop.get("name") or "").strip()
        if name:
            stop_names.append(name)
    if stop_names:
        start_name = stop_names[0]
        if len(stop_names) > 1:
            end_name = stop_names[-1]

    guidance = str(props.get("guidance") or "").strip()
    return AccessStep(
        mode=mode,
        duration_seconds=duration,
        distance_meters=distance,
        guidance=guidance,
        line_name=line_name,
        start_name=start_name,
        end_name=end_name,
    )
