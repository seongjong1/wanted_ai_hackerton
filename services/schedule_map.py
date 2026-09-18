"""Phase 5 map rendering — pydeck only, no network I/O.

TextLayer labels must be browser-visible: ASCII-safe glyphs, pixel size units,
and dark text (white-on-light maps hide labels).
"""
from __future__ import annotations

from typing import Any

from services.schedule_day_view import DayView, MapMarker

# RGBA by role — destination-day map only.
ROLE_COLORS: dict[str, list[int]] = {
    "ARRIVAL_HUB": [30, 136, 229, 220],
    "START": [30, 136, 229, 220],
    "ACTIVITY": [67, 160, 71, 220],
    "MAIN": [245, 124, 0, 240],
    "ACCOMMODATION": [123, 31, 162, 220],
    "PROVISIONAL": [255, 152, 0, 220],
    "RETURN_HUB": [0, 151, 167, 220],
}


def marker_label_text(marker: MapMarker) -> str:
    """ASCII-safe on-map label. Activity sequence matches Timeline numbering.

    MAIN keeps its visit number and adds a MAIN tag (no Unicode star — unstable in WebGL fonts).
    Hub / lodging / provisional never receive activity numbers.
    """
    if marker.sequence is not None:
        if marker.is_main or marker.role == "MAIN":
            return f"{marker.sequence} MAIN"
        return str(marker.sequence)
    if marker.role == "PROVISIONAL":
        return "P"
    if marker.role == "ACCOMMODATION":
        return "S"
    if marker.role in {"ARRIVAL_HUB", "START"}:
        return "H"
    if marker.role == "RETURN_HUB":
        return "R"
    return ""


def _marker_rows(markers: tuple[MapMarker, ...]) -> list[dict[str, Any]]:
    rows = []
    for marker in markers:
        is_main = marker.is_main or marker.role == "MAIN"
        text = marker_label_text(marker)
        display = f"MAIN · {marker.name}" if is_main else marker.name
        rows.append({
            "lat": marker.latitude,
            "lon": marker.longitude,
            "name": display,
            "role": marker.role,
            "text": text,
            "is_main": is_main,
            "color": ROLE_COLORS.get(marker.role, [97, 97, 97, 220]),
            # Larger radius so digit / "N MAIN" sits inside the circle.
            "radius": 160 if is_main else (120 if marker.sequence is not None else 90),
        })
    return rows


def _text_layer(pdk, data: list[dict[str, Any]], *, size: int, color: list[int],
                pixel_offset: list[int] | None = None):
    """Shared TextLayer settings that render reliably in Streamlit browsers."""
    from pydeck.types import String

    kwargs: dict[str, Any] = {
        "data": data,
        "get_position": "[lon, lat]",
        "get_text": "text",
        "get_size": size,
        # Literal enum — bare "pixels" is treated as a data column (@@=pixels) and labels vanish.
        "size_units": String("pixels"),
        "get_color": color,
        "get_text_anchor": String("middle"),
        "get_alignment_baseline": String("center"),
        "billboard": True,
        "pickable": False,
        "background": True,
        "get_background_color": [255, 255, 255, 230],
        "background_padding": [4, 2, 4, 2],
    }
    if pixel_offset is not None:
        kwargs["get_pixel_offset"] = pixel_offset
    return pdk.Layer("TextLayer", **kwargs)


def build_day_deck(view: DayView):
    """Return a pydeck.Deck or None when there are no plottable markers."""
    import pydeck as pdk

    rows = _marker_rows(view.markers)
    if not rows:
        return None
    layers: list[Any] = []
    path_coords = view.order_line
    if len(view.path_sequence) >= 2:
        path_coords = tuple((p.latitude, p.longitude) for p in view.path_sequence)
    if len(path_coords) >= 2:
        path = [[lon, lat] for lat, lon in path_coords]
        layers.append(pdk.Layer(
            "PathLayer",
            data=[{"path": path}],
            get_path="path",
            get_width=4,
            get_color=[96, 125, 139, 200],
            width_min_pixels=3,
        ))
    # Circles first, then labels on top.
    layers.append(pdk.Layer(
        "ScatterplotLayer",
        data=rows,
        get_position="[lon, lat]",
        get_fill_color="color",
        get_radius="radius",
        radius_min_pixels=10,
        radius_max_pixels=28,
        pickable=True,
    ))
    # Activity / hub labels (includes "1 MAIN" for MAIN places).
    labeled = [r for r in rows if r["text"]]
    if labeled:
        layers.append(_text_layer(
            pdk, labeled, size=16, color=[33, 33, 33, 255]))
    lats = [r["lat"] for r in rows]
    lons = [r["lon"] for r in rows]
    for lat, lon in path_coords:
        lats.append(lat)
        lons.append(lon)
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
