"""Phase 5 map rendering — pydeck only, no network I/O."""
from __future__ import annotations

from typing import Any

from services.schedule_day_view import DayView, MapMarker

# RGBA by role — destination-day map only.
ROLE_COLORS: dict[str, list[int]] = {
    "ARRIVAL_HUB": [30, 136, 229, 200],
    "START": [30, 136, 229, 200],
    "ACTIVITY": [67, 160, 71, 200],
    "MAIN": [245, 124, 0, 230],
    "ACCOMMODATION": [123, 31, 162, 210],
    "PROVISIONAL": [255, 152, 0, 210],
    "RETURN_HUB": [0, 151, 167, 210],
}


def _marker_rows(markers: tuple[MapMarker, ...]) -> list[dict[str, Any]]:
    rows = []
    for marker in markers:
        text = str(marker.sequence) if marker.sequence is not None else (
            "★" if marker.is_main or marker.role == "MAIN" else
            "임" if marker.role == "PROVISIONAL" else
            "숙" if marker.role == "ACCOMMODATION" else
            "도" if marker.role in {"ARRIVAL_HUB", "START"} else
            "귀" if marker.role == "RETURN_HUB" else "·")
        rows.append({
            "lat": marker.latitude,
            "lon": marker.longitude,
            "name": marker.name,
            "role": marker.role,
            "text": text,
            "color": ROLE_COLORS.get(marker.role, [97, 97, 97, 200]),
            "radius": 110 if marker.role == "MAIN" else 85,
        })
    return rows


def build_day_deck(view: DayView):
    """Return a pydeck.Deck or None when there are no plottable markers."""
    import pydeck as pdk

    rows = _marker_rows(view.markers)
    if not rows:
        return None
    layers: list[Any] = []
    if len(view.order_line) >= 2:
        # Visit-order guide only — not claimed as real transit geometry.
        path = [[lon, lat] for lat, lon in view.order_line]
        layers.append(pdk.Layer(
            "PathLayer",
            data=[{"path": path}],
            get_path="path",
            get_width=3,
            get_color=[120, 144, 156, 160],
            width_min_pixels=2,
        ))
    layers.append(pdk.Layer(
        "ScatterplotLayer",
        data=rows,
        get_position="[lon, lat]",
        get_fill_color="color",
        get_radius="radius",
        radius_min_pixels=6,
        radius_max_pixels=18,
        pickable=True,
    ))
    layers.append(pdk.Layer(
        "TextLayer",
        data=rows,
        get_position="[lon, lat]",
        get_text="text",
        get_size=14,
        get_color=[255, 255, 255, 255],
        get_alignment_baseline="'center'",
        get_text_anchor="'middle'",
    ))
    lats = [r["lat"] for r in rows]
    lons = [r["lon"] for r in rows]
    view_state = pdk.ViewState(
        latitude=sum(lats) / len(lats),
        longitude=sum(lons) / len(lons),
        zoom=_zoom_for_span(lats, lons),
        pitch=0,
    )
    return pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        tooltip={"text": "{name}\n{role}"},
        map_style=None,
    )


def _zoom_for_span(lats: list[float], lons: list[float]) -> float:
    span = max(max(lats) - min(lats), max(lons) - min(lons), 0.002)
    if span < 0.01:
        return 13.5
    if span < 0.03:
        return 12.5
    if span < 0.08:
        return 11.5
    return 10.5
