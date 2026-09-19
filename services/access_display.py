"""UI-only access labels. No schedule or boarding logic."""


def is_trivial_same_place_access(leg: object) -> bool:
    """True when access is same-place / ~0 minutes — hide as travel in UI only.

    Does not change boarding buffers or long-distance departure times.
    """
    if leg is None:
        return False
    try:
        duration = float(getattr(leg, "duration_minutes", 0) or 0)
    except (TypeError, ValueError):
        return False
    if duration > 0.5:
        return False
    modes = getattr(leg, "transport_modes", ()) or ()
    try:
        if "SAME_PLACE" in modes:
            return True
    except TypeError:
        pass
    origin_point = getattr(leg, "origin_point", None)
    dest_point = getattr(leg, "destination_point", None)
    origin_id = getattr(origin_point, "id", "") or ""
    dest_id = getattr(dest_point, "id", "") or ""
    if origin_id and dest_id and origin_id == dest_id:
        return True
    origin = str(getattr(leg, "origin", "") or "").strip()
    dest = str(getattr(leg, "destination", "") or "").strip()
    if origin and dest and origin == dest:
        return True
    return duration <= 0


def access_hub_label(leg: object) -> str:
    """Resolved hub name for UI — never invent a fake origin→hub hop."""
    dest_point = getattr(leg, "destination_point", None)
    name = str(getattr(dest_point, "name", "") or "").strip() if dest_point is not None else ""
    if name:
        return name
    dest = str(getattr(leg, "destination", "") or "").strip()
    if dest:
        return dest
    origin_point = getattr(leg, "origin_point", None)
    name = str(getattr(origin_point, "name", "") or "").strip() if origin_point is not None else ""
    if name:
        return name
    return str(getattr(leg, "origin", "") or "").strip()
