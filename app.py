"""Collect a domestic trip and compare real long-distance transport candidates."""
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import streamlit as st
from pydantic import ValidationError
from streamlit.errors import StreamlitSecretNotFoundError

from config import Settings, load_settings
from models.trip_request import Preference, RADIUS_OPTIONS, TripRequest
from services.health_service import check_connections
from services.transport_service import search_transport
from models.access import is_estimated_access
from models.transport import TransportCandidate, TransportType
from services.access_display import access_hub_label, is_trivial_same_place_access
from services.place_service import search_places_for_trip, place_reason, activity_radius_meters


def read_settings() -> Settings:
    try:
        return load_settings(st.secrets)
    except StreamlitSecretNotFoundError:
        return load_settings()


def render_trip_form() -> None:
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    with st.form("trip_form"):
        st.subheader("여행 기본정보")
        left, right = st.columns(2)
        departure = left.text_input("출발지", placeholder="역명, 건물명 또는 도로명 주소", max_chars=200)
        destination = right.text_input("여행지", placeholder="예: 부산 해운대", max_chars=200)
        start_date = left.date_input("여행 시작 날짜", value=today)
        end_date = right.date_input("여행 종료 날짜", value=today)
        departure_time = left.time_input("출발 희망 시간", value=time(9))
        end_time = right.time_input("귀가 완료 목표 시간", value=time(20))
        # Multi-day (end_date > start_date) always requires overnight lodging.
        # Same-day trips never need an accommodation section.
        if end_date > start_date:
            st.caption("1박 이상 여행입니다. 숙소는 아래에서 정함/미정만 선택합니다.")
        else:
            st.caption("당일 여행 · 숙소 선택이 필요하지 않습니다.")
        st.caption("국내 여행 · 모든 날짜와 시간은 한국 시각 기준입니다.")

        st.subheader("동행 및 조건")
        allergies = st.text_input("음식 알레르기", placeholder="예: 땅콩, 갑각류 (쉼표로 구분)",
                                  max_chars=1000)
        left, right = st.columns(2)
        has_pet = left.checkbox("반려동물 동반")
        has_child = right.checkbox("아이 동반")
        activity_radius = st.selectbox("활동반경", RADIUS_OPTIONS, index=4)
        st.caption("활동반경은 선택 장소 주변의 추가 검색에 적용됩니다. ‘500m 이상’은 설정된 반경 상한을 사용합니다.")

        st.subheader("여행 성향")
        preferences = st.multiselect("선호하는 여행을 선택하세요 (최소 1개)",
                                     [p.value for p in Preference])
        submitted = st.form_submit_button("여행 계획 생성", type="primary", use_container_width=True)

    if submitted:
        for key in ("trip_request", "transport_result", "transport_candidates", "selected_transport",
                    "place_result", "place_candidates", "place_anchor", "nearby_result", "trip_schedule",
                    "schedule_result", "schedule_preferred_place_id", "user_selected_place_id",
                    "main_place_list_expanded",
                    "schedule_anchor_pending", "origin_address_input", "origin_address_error",
                    "accommodation_query", "accommodation_choice", "accommodation_point",
                    "accommodation_error", "accommodation_resolved_query", "provisional_hub_name",
                    "accommodation_source", "accommodation_recommend_result",
                    "accommodation_recommend_signature",
                    "pending_replan", "pending_confirmation", "replan_user_text",
                    "replan_base_signature", "replan_applied_id", "replan_through_key",
                    "replan_progress_key", "replan_visit_status",
                    "replan_current_date", "replan_current_time"):
            st.session_state.pop(key, None)
        try:
            trip = TripRequest(
                departure=departure, destination=destination, start_date=start_date,
                end_date=end_date, departure_time=departure_time, end_time=end_time,
                has_accommodation=(end_date > start_date), allergies=tuple(allergies.split(",")),
                has_pet=has_pet, has_child=has_child, activity_radius=activity_radius,
                preferences=tuple(preferences),
            )
        except ValidationError as exc:
            labels = {"departure": "출발지를 입력하세요.", "destination": "여행지를 입력하세요.",
                      "preferences": "여행 성향을 최소 1개 선택하세요.",
                      "activity_radius": "활동반경을 확인하세요."}
            for error in exc.errors(include_input=False, include_url=False):
                field = error["loc"][0] if error["loc"] else ""
                st.error(labels.get(field, error["msg"].removeprefix("Value error, ")))
        else:
            st.session_state.trip_request = trip
            with st.spinner("역·터미널과 교통편을 조회하고 있습니다…"):
                result = search_transport(trip, read_settings())
            st.session_state.transport_result = result
            st.session_state.transport_candidates = result.candidates
            st.session_state.selected_transport = None

    if "trip_request" in st.session_state:
        st.success("여행 조건을 확인했습니다.")
        render_accommodation_panel()
        render_origin_address_form()
        render_transport_results()


def _clear_replan_state() -> None:
    for key in ("pending_replan", "pending_confirmation", "replan_user_text",
                "replan_base_signature", "replan_applied_id", "replan_through_key",
                "replan_progress_key", "replan_visit_status",
                "replan_progress_select", "replan_visit_status_radio",
                "replan_through_select", "replan_current_date", "replan_current_time",
                "replan_confirm_choice"):
        st.session_state.pop(key, None)


def _stale_accommodation_schedule() -> None:
    for key in ("trip_schedule", "schedule_result", "schedule_signature"):
        st.session_state.pop(key, None)
    _clear_replan_state()


def _clear_recommended_accommodation() -> None:
    st.session_state.pop("accommodation_point", None)
    st.session_state.pop("accommodation_source", None)
    st.session_state.pop("accommodation_resolved_query", None)


def _ensure_accommodation_recommendations() -> None:
    """Search lodging candidates once per destination+hub; failures stay local."""
    trip = st.session_state.trip_request
    selected = st.session_state.get("selected_transport")
    if selected is None:
        return
    from services.multiday_schedule_service import arrival_hub_query
    signature = f"{trip.destination}|{arrival_hub_query(selected)}|{selected.arrival_time.isoformat()}"
    if st.session_state.get("accommodation_recommend_signature") == signature:
        return
    from services.accommodation_recommend_service import search_accommodation_candidates
    preferred_id = (st.session_state.get("user_selected_place_id")
                    or st.session_state.get("schedule_preferred_place_id"))
    main = st.session_state.get("place_result")
    pool = list(main.candidates) if main else []
    preferred = next((c for c in pool if c.place_id == preferred_id), None)
    with st.spinner("여행 동선 기준 숙소 후보를 찾고 있습니다…"):
        result = search_accommodation_candidates(
            trip, selected, read_settings(),
            preferred=preferred,
            schedule=st.session_state.get("trip_schedule"),
            place_pool=pool)
    st.session_state.accommodation_recommend_result = result
    st.session_state.accommodation_recommend_signature = signature


def render_accommodation_panel() -> None:
    trip = st.session_state.trip_request
    from services.multiday_schedule_service import is_multiday
    if not is_multiday(trip):
        return
    st.subheader("숙소")
    st.caption("1박 이상 여행은 숙박이 필요합니다. 숙소를 정했는지 여부만 선택하세요.")
    choice = st.radio(
        "숙소는 정하셨나요?",
        ("숙소를 정했어요", "아직 숙소를 정하지 않았어요"),
        index=None if st.session_state.get("accommodation_choice") is None else (
            0 if st.session_state.get("accommodation_choice") == "known" else 1),
        key="accommodation_choice_radio",
    )
    previous = st.session_state.get("accommodation_choice")
    if choice == "숙소를 정했어요":
        if previous != "known":
            st.session_state.accommodation_choice = "known"
            _clear_recommended_accommodation()
            st.session_state.pop("accommodation_error", None)
            st.session_state.accommodation_source = "manual"
            _stale_accommodation_schedule()
        st.text_input(
            "숙소명 또는 주소를 입력하세요",
            key="accommodation_query",
            placeholder="예: 구미센츄리호텔, 경북 구미시 ○○로 12",
            max_chars=200,
            help="숙소 위치는 다음 날 출발 위치와 하루 일정의 종료 지점을 계산하는 데 사용됩니다.",
        )
        query = (st.session_state.get("accommodation_query") or "").strip()
        if st.button("숙소 위치 확인", key="confirm_accommodation", disabled=not query):
            from services.schedule_service import resolve_trip_accommodation
            with st.spinner("숙소 위치를 확인하고 있습니다…"):
                point, error = resolve_trip_accommodation(query, read_settings())
            if point is None:
                st.session_state.accommodation_error = error
                st.session_state.pop("accommodation_point", None)
                st.session_state.pop("accommodation_resolved_query", None)
            else:
                st.session_state.accommodation_point = point
                st.session_state.accommodation_resolved_query = query
                st.session_state.accommodation_source = "manual"
                st.session_state.pop("accommodation_error", None)
                _stale_accommodation_schedule()
            st.rerun()
        if st.session_state.get("accommodation_error"):
            st.error(st.session_state.accommodation_error)
        point = st.session_state.get("accommodation_point")
        resolved_query = st.session_state.get("accommodation_resolved_query")
        if point is not None and resolved_query == query:
            with st.container(border=True):
                st.write("**숙소 위치 확인**")
                st.write(point.name)
                if point.address:
                    st.caption(point.address)
        elif query:
            st.caption("입력한 숙소명을 확인하려면 [숙소 위치 확인]을 눌러주세요.")
    elif choice == "아직 숙소를 정하지 않았어요":
        if previous != "unknown":
            st.session_state.accommodation_choice = "unknown"
            _clear_recommended_accommodation()
            st.session_state.pop("accommodation_error", None)
            st.session_state.pop("accommodation_recommend_result", None)
            st.session_state.pop("accommodation_recommend_signature", None)
            _stale_accommodation_schedule()
        st.info(
            "숙소를 선택하지 않으면 도착 거점을 임시 기준점으로 일정을 계산합니다. "
            "아래에서 추천 숙소 후보를 고르면 전체 숙박일 기준으로 일정을 다시 계산합니다."
        )
        selected = st.session_state.get("selected_transport")
        if selected is None:
            st.caption("교통편을 선택한 뒤 여행 동선 기준 숙소 후보를 보여드립니다.")
        else:
            _ensure_accommodation_recommendations()
            result = st.session_state.get("accommodation_recommend_result")
            point = st.session_state.get("accommodation_point")
            if point is not None and st.session_state.get("accommodation_source") == "recommended":
                st.markdown("### 선택한 숙소")
                with st.container(border=True):
                    st.write(f"**{point.name}**")
                    if point.address:
                        st.caption(point.address)
                    st.caption("이 숙소를 전체 숙박일의 기준으로 사용합니다.")
                if st.button("다른 숙소 후보 보기", key="clear_recommended_accommodation"):
                    _clear_recommended_accommodation()
                    _stale_accommodation_schedule()
                    st.rerun()
            else:
                st.markdown("### 추천 숙소 후보")
                if result is None:
                    st.caption("숙소 후보를 준비하지 못했습니다.")
                else:
                    for notice in result.notices:
                        st.caption(notice)
                    if result.status == "failed" or result.status == "empty":
                        pass
                    elif result.candidates:
                        for candidate in result.candidates:
                            with st.container(border=True):
                                st.write(f"**{candidate.place_name}**")
                                if candidate.address:
                                    st.caption(candidate.address)
                                if candidate.reason:
                                    st.caption(candidate.reason)
                                st.caption("가격·객실 정보는 예약 서비스에서 확인 필요")
                                if st.button("이 숙소 선택", key=f"select_lodging_{candidate.place_id}"):
                                    st.session_state.accommodation_point = candidate.as_access_point()
                                    st.session_state.accommodation_source = "recommended"
                                    st.session_state.accommodation_resolved_query = candidate.place_name
                                    _stale_accommodation_schedule()
                                    st.rerun()
                if st.button("아직 선택하지 않고 임시 일정 보기", key="skip_lodging_recommend"):
                    _clear_recommended_accommodation()
                    _stale_accommodation_schedule()
                    st.rerun()
    else:
        st.session_state.accommodation_choice = None
        st.caption("숙박 일정을 만들려면 숙소 여부를 선택해주세요.")


def render_origin_address_form() -> None:
    result = st.session_state.get("transport_result")
    if result is None or result.origin_resolution_status != "NEED_ADDRESS":
        return
    with st.form("origin_address_form"):
        st.caption("출발지 장소명을 정확히 찾지 못했습니다. 도로명 주소 또는 지번 주소를 입력해주세요.")
        address = st.text_input("출발지 주소", key="origin_address_input", max_chars=300)
        submitted = st.form_submit_button("주소로 교통편 다시 조회", key="submit_origin_address")
    if submitted:
        if not address.strip():
            st.session_state.origin_address_error = "도로명 주소 또는 지번 주소를 입력해주세요."
        else:
            with st.spinner("주소를 확인하고 교통편을 조회하고 있습니다…"):
                updated = search_transport(st.session_state.trip_request, read_settings(), origin_address=address.strip())
            if updated.origin_resolution_status == "RESOLVED":
                st.session_state.transport_result = updated
                st.session_state.transport_candidates = updated.candidates
                for key in ("selected_transport", "place_result", "place_candidates", "nearby_result", "trip_schedule", "schedule_result", "origin_address_error"):
                    st.session_state.pop(key, None)
                _clear_replan_state()
                st.rerun()
            else:
                st.session_state.origin_address_error = updated.user_message or "주소를 확인하지 못했습니다. 더 구체적인 주소를 입력해주세요."
    if st.session_state.get("origin_address_error"):
        st.error(st.session_state.origin_address_error)


TRANSPORT_LABELS = {TransportType.TRAIN: "열차", TransportType.EXPRESS_BUS: "고속버스",
                    TransportType.INTERCITY_BUS: "시외버스"}


def format_minutes(value: float) -> str:
    return f"{value:g}분" if float(value).is_integer() else f"약 {int(value + 0.5)}분"


def render_candidate(candidate: TransportCandidate, number: int, *, selectable: bool) -> None:
    with st.container(border=True):
        label = f"추천 {number} · " if selectable else ""
        st.write(f"**{label}{TRANSPORT_LABELS[candidate.transport_type]} · {candidate.grade or '등급 정보 없음'}**")
        st.write(f"{candidate.departure_place} → {candidate.arrival_place}")
        st.write(f"{candidate.departure_time:%m/%d %H:%M} 출발 → {candidate.arrival_time:%m/%d %H:%M} 도착")
        price = f"{candidate.price:,.0f}원" if candidate.price is not None else "정보 없음"
        st.write(f"소요시간 {format_minutes(candidate.duration_minutes)} · 성인 요금 {price}")
        if candidate.access is not None:
            boarding = candidate.access
            leg = boarding.access_leg
            modes = {"BUS": "버스", "SUBWAY": "지하철", "WALKING": "도보",
                     "SAME_PLACE": "같은 장소", "ESTIMATED": "추정"}
            estimated_access = is_estimated_access(leg)
            trivial_access = is_trivial_same_place_access(leg)
            if trivial_access:
                hub = access_hub_label(leg) or candidate.departure_place
                st.write(f"접근 이동 없음 · {hub}에서 바로 출발")
            elif estimated_access:
                st.write(f"접근 이동(추정): {leg.origin} → {leg.destination} · {format_minutes(leg.duration_minutes)}")
                st.caption("실제 대중교통 경로가 아닙니다. 경로 API를 확인하지 못해 직선거리 기반 추정 시간을 사용합니다.")
            else:
                st.write(f"접근 이동: {leg.origin} → {leg.destination} · {format_minutes(leg.duration_minutes)}")
                st.caption(" + ".join(modes.get(mode, mode) for mode in getattr(leg, "transport_modes", ()))
                           + f" · 환승 {getattr(leg, 'transfers', 0)}회")
            if not trivial_access:
                st.write(f"출발 가능 {leg.departure_time:%H:%M} → 거점 도착 {leg.arrival_time:%H:%M:%S}"
                         f" → 승차 준비 완료 {boarding.ready_time:%H:%M:%S}")
            st.write(f"승차 버퍼 {format_minutes(boarding.buffer_minutes)} · 추가 대기 {format_minutes(boarding.waiting_minutes)}"
                     f" · 총 소요시간 {format_minutes(boarding.total_duration_minutes)}")
            steps = [step for step in getattr(leg, "steps", ()) if getattr(step, "mode", "").strip() and
                     (getattr(step, "duration_seconds", 0) > 0 or getattr(step, "distance_meters", 0) > 0
                      or getattr(step, "guidance", "").strip())]
            if steps and not estimated_access and not trivial_access:
                with st.expander("접근 경로 자세히 보기", expanded=False):
                    for step in steps:
                        st.text(f"{modes.get(step.mode, step.mode)} · {format_minutes(step.duration_seconds / 60)}"
                                f" · {step.distance_meters:g}m · {step.guidance}")
            elif estimated_access:
                st.caption(getattr(leg, "note", "") or "추정 접근 시간")
        if candidate.train_number:
            st.caption(f"열차 번호 {candidate.train_number}")
        if selectable and st.button("이 교통편 선택", key=f"select_transport_{number}"):
            for key in ("place_result", "place_candidates", "place_anchor", "nearby_result", "trip_schedule", "schedule_result", "schedule_preferred_place_id", "user_selected_place_id", "main_place_list_expanded", "schedule_anchor_pending", "origin_address_input", "origin_address_error"):
                st.session_state.pop(key, None)
            st.session_state.selected_transport = candidate
            for key in ("schedule_signature", "nearby_point_choice"):
                st.session_state.pop(key, None)
            _clear_replan_state()
            st.rerun()


def render_transport_results() -> None:
    result = st.session_state.get("transport_result")
    if result is None:
        return
    selected = st.session_state.get("selected_transport")
    if not selected:
        st.subheader("추천 교통편")
        st.caption("가장 적합한 이동 방법을 선택하세요.")
    if result.user_message:
        st.info(result.user_message)
    elif not result.candidates:
        st.info("조건에 맞는 가는 교통편을 찾지 못했습니다. 장소명·날짜·시간을 조정하거나 잠시 후 다시 조회하세요.")
    if not selected:
        for number, candidate in enumerate(result.candidates[:5], 1):
            render_candidate(candidate, number, selectable=True)
    if selected:
        st.subheader("선택한 교통편")
        render_candidate(selected, 1, selectable=False)
        st.caption("운행정보 확인됨 · 좌석/매진 여부는 예매처에서 확인 필요")
        alternatives = [(n, c) for n, c in enumerate(result.candidates[:5], 1) if c != selected]
        if alternatives:
            with st.expander(f"다른 추천 교통편 보기 ({len(alternatives)}개)", expanded=False):
                for number, candidate in alternatives:
                    render_candidate(candidate, number, selectable=True)
        render_place_results()


def render_place_results() -> None:
    st.subheader("주변 추천 장소")
    st.caption("메인 장소는 여행지역 전체에서, 주변 장소는 선택한 기준점의 활동반경 안에서 찾습니다.")
    previous = st.session_state.get("place_result")
    anchor_id = None
    if previous and previous.anchor_candidates and not previous.destination_scope:
        anchors = {a.id:a for a in previous.anchor_candidates}
        anchor_id = st.selectbox("장소 검색 기준점", list(anchors),
                                 format_func=lambda value: anchors[value].name, key="place_anchor")
    if st.button("주요 장소 검색", key="search_nearby_places"):
        with st.spinner("실제 주변 장소와 성향 적합도를 확인하고 있습니다…"):
            result = search_places_for_trip(st.session_state.trip_request, st.session_state.selected_transport,
                                           read_settings(), anchor_id)
        st.session_state.pop("nearby_result", None)
        st.session_state.pop("trip_schedule", None)
        st.session_state.pop("schedule_result", None)
        _clear_replan_state()
        st.session_state.place_result = result
        st.session_state.place_candidates = result.candidates
        # Fresh candidate set: keep MAIN only if it still exists; otherwise expand list.
        selected_id = st.session_state.get("user_selected_place_id")
        ids = {c.place_id for c in result.candidates}
        if selected_id and selected_id not in ids:
            st.session_state.pop("user_selected_place_id", None)
            st.session_state.pop("schedule_preferred_place_id", None)
            st.session_state.main_place_list_expanded = True
        elif not selected_id:
            st.session_state.main_place_list_expanded = True
        st.rerun()
    result = st.session_state.get("place_result")
    if result is None:
        return
    if result.destination_scope:
        st.subheader(f"{result.destination_scope} 주요 추천 장소")
        for notice in result.notices:
            st.info(notice)
        _render_main_place_section(result)
    else:
        st.write(f"**{result.anchor.name if result.anchor else '기준점 미확인'} 주변 · {result.status}**")
        for notice in result.notices:
            st.info(notice)
        render_place_cards(result, allow_anchor=True)
    if result.destination_scope:
        from models.access import AccessPoint
        points = {c.place_id: AccessPoint(id=c.place_id, name=c.place_name, x=c.longitude, y=c.latitude)
                  for c in result.candidates}
        if result.anchor:
            points[result.anchor.id] = result.anchor
        if points:
            with st.expander("이 장소 주변 직접 둘러보기", expanded=False):
                radius = activity_radius_meters(st.session_state.trip_request.activity_radius, read_settings().extended_activity_radius_meters)
                st.caption(f"주변 검색 범위: 최대 {radius / 1000:g}km" if radius >= 1000 else f"주변 검색 범위: 최대 {radius}m")
                chosen = st.selectbox("주변 검색 기준점", list(points), format_func=lambda key: points[key].name,
                                      key="nearby_point_choice", on_change=schedule_place_changed)
                st.caption("주변 검색 기준점은 일정 포함을 뜻하지 않습니다.")
                if st.button("이 장소 주변 검색", key="search_additional_places"):
                    st.session_state.pop("trip_schedule", None)
                    st.session_state.pop("schedule_result", None)
                    st.session_state.nearby_result = search_places_for_trip(
                        st.session_state.trip_request, st.session_state.selected_transport, read_settings(),
                        nearby=True, point=points[chosen])
                nearby = st.session_state.get("nearby_result")
                if nearby:
                    st.caption(f"{nearby.anchor.name if nearby.anchor else '기준점 미확인'} 주변 · 반경 {nearby.radius_meters:,}m")
                    for notice in nearby.notices:
                        st.info(notice)
                    render_place_cards(nearby)

    render_schedule_results()


def _selected_main_candidate(result):
    selected_id = st.session_state.get("user_selected_place_id")
    if not selected_id:
        return None
    return next((c for c in result.candidates if c.place_id == selected_id), None)


def _main_place_list_should_expand(selected) -> bool:
    """Collapse only after an explicit MAIN pick; expand when changing or unset."""
    if selected is None:
        return True
    return bool(st.session_state.get("main_place_list_expanded"))


def _render_main_place_section(result) -> None:
    selected = _selected_main_candidate(result)
    if selected is not None and not _main_place_list_should_expand(selected):
        st.markdown("### 선택한 기준 장소")
        with st.container(border=True):
            st.write(f"**{selected.place_name}**")
            st.text(selected.category or "카테고리 정보 없음")
            st.text(selected.address or "주소 정보 없음")
            reason = place_reason(selected) or selected.ai_reason
            if reason and "관련 검색에서 찾은 후보입니다" not in reason:
                st.caption("추천 근거: " + reason)
        if st.button("다른 장소로 변경", key="expand_main_place_list"):
            st.session_state.main_place_list_expanded = True
            st.rerun()
        return
    if selected is not None:
        st.caption(f"현재 기준 장소: {selected.place_name} · 아래에서 다른 장소를 선택할 수 있습니다.")
    render_place_cards(result, allow_anchor=True)


def render_place_cards(result, *, show_all: bool = False, allow_anchor: bool = False) -> None:
    if not result.candidates:
        st.info("장소 후보가 없습니다. 여행지역이나 성향을 조정해 다시 검색하세요.")
    trip = st.session_state.trip_request
    for index, candidate in enumerate(result.candidates):
        from contextlib import nullcontext
        # Keep service order and complete candidates in session state.
        with st.expander("더 보기", expanded=False) if index == 6 and not show_all else nullcontext():
            if index == 6 and not show_all:
                from dataclasses import replace
                render_place_cards(replace(result, candidates=result.candidates[6:]), show_all=True, allow_anchor=allow_anchor)
                break
        with st.container(border=True):
            st.write(f"**{candidate.place_name}**")
            st.text(candidate.category or "카테고리 정보 없음")
            st.text(candidate.address or "주소 정보 없음")
            if result.anchor and candidate.distance_meters is not None:
                st.write(f"기준점에서 {candidate.distance_meters:.0f}m (직선거리)")

            if candidate.phone:
                st.text(f"전화: {candidate.phone}")
            if candidate.place_url:
                st.link_button("Kakao 장소 정보", candidate.place_url)
            reason = place_reason(candidate) or candidate.ai_reason
            if reason and "관련 검색에서 찾은 후보입니다" not in reason:
                st.caption("추천 근거: " + reason)
            conditions = []
            if trip.has_pet:
                conditions.append("반려동물: 미확인")
            if trip.has_child:
                conditions.append("아이 동반: 미확인")
            if trip.allergies:
                conditions.append("알레르기: 미확인")
            if conditions:
                st.caption(" · ".join(conditions))
            if allow_anchor and candidate.role == "MAIN_DESTINATION":
                if st.session_state.get("user_selected_place_id") == candidate.place_id:
                    st.caption("선택한 일정 기준 장소")
                if st.button("이 장소 중심으로 일정 만들기", key=f"anchor_schedule_{candidate.place_id}"):
                    st.session_state.user_selected_place_id = candidate.place_id
                    st.session_state.schedule_preferred_place_id = candidate.place_id
                    st.session_state.main_place_list_expanded = False
                    for key in ("trip_schedule", "schedule_result", "schedule_signature", "nearby_result"):
                        st.session_state.pop(key, None)
                    _clear_replan_state()
                    st.session_state.schedule_anchor_pending = True
                    st.rerun()


def schedule_place_changed() -> None:
    st.session_state.pop("user_selected_place_id", None)
    st.session_state.schedule_preferred_place_id = st.session_state.get("nearby_point_choice")
    st.session_state.main_place_list_expanded = True
    st.session_state.pop("trip_schedule", None)
    st.session_state.pop("schedule_result", None)
    _clear_replan_state()


def render_schedule_items(items, previous) -> object:
    modes = {"BUS": "버스", "SUBWAY": "지하철", "WALKING": "도보"}
    for item in items:
        if item.start_datetime > previous:
            st.caption(f"{previous:%m/%d %H:%M} ~ {item.start_datetime:%m/%d %H:%M} · 자유시간")
        with st.container(border=True):
            st.write(f"**{item.start_datetime:%m/%d %H:%M} ~ {item.end_datetime:%m/%d %H:%M}**")
            if item.item_type == "TRAVEL":
                st.write(f"{item.origin.name} → {item.destination.name}")
                label = "숙소 이동" if item.reason == "숙소 이동" else (
                    " + ".join(modes.get(m, m) for m in item.travel_mode) + " · 이동 예상 " + format_minutes(item.duration_minutes))
                st.caption(label if item.reason != "숙소 이동" else
                           " + ".join(modes.get(m, m) for m in item.travel_mode) + " · 숙소 이동 " + format_minutes(item.duration_minutes))
            else:
                st.write(item.place_name)
                st.caption("체류 추정 " + format_minutes(item.duration_minutes) + " · " + item.reason)
                for check in item.checks:
                    st.caption(check)
        previous = item.end_datetime
    return previous


def render_return_journey(schedule) -> None:
    from models.schedule import ReturnStatus
    if schedule.return_status == ReturnStatus.RETURN_UNKNOWN:
        st.warning("귀가 교통편을 확인하지 못했습니다. 가는 교통편은 계속 확인할 수 있습니다.")
    elif schedule.return_status == ReturnStatus.RETURN_INFEASIBLE:
        st.warning("가는 교통편은 조회되었지만 현재 귀가 완료 목표시간까지 가능한 왕복 일정을 만들기 어렵습니다.")
    elif schedule.return_status == ReturnStatus.RETURN_NONE and schedule.return_journey is None:
        st.info("현재 조건에서 귀가 교통편 후보를 찾지 못했습니다. 가는 교통편은 계속 확인할 수 있습니다.")
    if not schedule.return_journey:
        return
    journey = schedule.return_journey
    st.subheader("돌아가는 길")
    if schedule.destination_activity_cutoff:
        cutoff = schedule.destination_activity_cutoff
        st.caption(f"{cutoff:%m/%d} 목적지 활동 종료 권장 한도: {cutoff:%H:%M}")
        st.caption("이 시간 이후에는 귀가 거점 이동·승차 준비·귀가 교통 때문에 새 활동을 추가하지 않습니다.")
    for leg in (journey.to_hub,):
        st.write(f"{leg.departure_time:%m/%d %H:%M} ~ {leg.arrival_time:%m/%d %H:%M} · {leg.origin} → {leg.destination}")
    st.caption(f"승차 준비 {format_minutes(journey.boarding_buffer_minutes)} · 운행정보 확인됨 · 좌석/매진 여부는 예매처에서 확인 필요")
    transport = journey.transport
    st.write(f"{transport.departure_time:%m/%d %H:%M} ~ {transport.arrival_time:%m/%d %H:%M} · {TRANSPORT_LABELS[transport.transport_type]} · {transport.departure_place} → {transport.arrival_place}")
    leg = journey.to_origin
    st.write(f"{leg.departure_time:%m/%d %H:%M} ~ {leg.arrival_time:%m/%d %H:%M} · {leg.origin} → {leg.destination}")
    margin = (schedule.trip_end_datetime - schedule.final_arrival_datetime).total_seconds() / 60
    st.success(f"귀가 완료 목표 {schedule.trip_end_datetime:%m/%d %H:%M} · 예상 귀가 {schedule.final_arrival_datetime:%m/%d %H:%M} · 남은 여유 {format_minutes(margin)}")


def render_schedule_results() -> None:
    from services.schedule_service import generate_trip_schedule
    from services.multiday_schedule_service import arrival_hub_query, is_multiday
    import hashlib
    trip = st.session_state.trip_request
    selected = st.session_state.selected_transport
    main = st.session_state.get("place_result")
    nearby = st.session_state.get("nearby_result")
    candidates = list(main.candidates) if main else []
    if nearby:
        candidates += nearby.candidates
    preferred = st.session_state.get("user_selected_place_id") or st.session_state.get("schedule_preferred_place_id")
    if preferred not in {c.place_id for c in candidates}:
        preferred = None
    lodging_choice = st.session_state.get("accommodation_choice")
    accommodation_point = st.session_state.get("accommodation_point")
    accommodation_query = (st.session_state.get("accommodation_query") or "").strip()
    resolved_query = st.session_state.get("accommodation_resolved_query")
    accommodation_source = st.session_state.get("accommodation_source")
    manual_confirmed = (lodging_choice == "known" and accommodation_point is not None
                        and resolved_query == accommodation_query)
    recommended_confirmed = (
        lodging_choice == "unknown" and accommodation_point is not None
        and accommodation_source == "recommended")
    confirmed = manual_confirmed or recommended_confirmed
    undecided = lodging_choice == "unknown" and not recommended_confirmed
    lodging_key = (
        f"unknown|{arrival_hub_query(selected)}" if undecided else
        f"known|{getattr(accommodation_point, 'id', '')}|{resolved_query or ''}|{accommodation_source or ''}"
        if confirmed else
        f"pending|{accommodation_query}"
    )
    signature = hashlib.sha256((trip.model_dump_json() + selected.model_dump_json() +
        "".join(c.model_dump_json() for c in candidates) + str(preferred) + lodging_key).encode()).hexdigest()
    if st.session_state.get("schedule_signature") != signature:
        st.session_state.pop("trip_schedule", None)
        st.session_state.pop("schedule_result", None)
        _clear_replan_state()
    st.subheader("추천 여행 일정")
    anchor_name = next((c.place_name for c in candidates if c.place_id == preferred), "자동 추천")
    st.write(f"기준 장소: {anchor_name}")
    if trip.end_date > trip.start_date:
        st.write(f"여행 기간: {trip.start_date:%m/%d} ~ {trip.end_date:%m/%d}")
    st.write(f"귀가 목표: {trip.end_date:%m/%d} {trip.end_time:%H:%M} · {trip.departure}")
    if confirmed:
        st.write(f"숙소: {accommodation_point.name}")
        if accommodation_source == "recommended":
            st.caption("선택한 추천 숙소를 전체 숙박일의 기준으로 사용합니다.")
    elif undecided:
        schedule_preview = st.session_state.get("trip_schedule")
        if (schedule_preview and schedule_preview.accommodation
                and schedule_preview.accommodation.name):
            hub_name = schedule_preview.accommodation.name
            st.session_state.provisional_hub_name = hub_name
        elif st.session_state.get("provisional_hub_name"):
            hub_name = st.session_state.provisional_hub_name
        else:
            # Resolve Kakao canonical hub name once (avoid TAGO short query label like 구미버스터미널).
            try:
                from services.access_service import AccessService
                from providers.http_client import HttpClient
                from providers.kakao_provider import KakaoProvider
                from providers.kakao_transit_provider import KakaoTransitProvider
                settings_local = read_settings()
                http = HttpClient(max_attempts=1)
                try:
                    access = AccessService(
                        KakaoProvider(settings_local.kakao_rest_api_key, http),
                        KakaoTransitProvider(settings_local.kakao_rest_api_key, http))
                    hub_name = access.resolve_point(arrival_hub_query(selected)).name
                    st.session_state.provisional_hub_name = hub_name
                finally:
                    http.close()
            except Exception:
                hub_name = arrival_hub_query(selected)
        st.write(f"숙소: 미정 · 임시 기준점: {hub_name}")
    with st.expander("일정 계산 기준 안내", expanded=False):
        st.caption("일정 사이의 자유시간에는 가능한 경우 주변 관광·카페·휴식 장소를 추천하며, 적절한 후보가 없을 때만 자유시간으로 남깁니다.")
        st.caption("체류시간은 기본 추정값입니다. 영업시간과 실제 배차·지연은 방문 전에 확인해주세요.")
        if undecided:
            st.caption("숙소를 선택하지 않으면 도착 거점을 임시 기준점으로 사용하며, 추천 숙소 또는 직접 입력 숙소를 확인하면 전체 일정을 다시 계산합니다.")
    multiday = is_multiday(trip)
    can_generate = True
    if multiday:
        if lodging_choice is None:
            can_generate = False
            st.caption("위쪽 [숙소]에서 숙소 여부를 먼저 선택해주세요.")
        elif lodging_choice == "known" and not confirmed:
            can_generate = False
            st.caption("위쪽 [숙소]에서 숙소 위치를 확인한 뒤 일정을 생성해주세요.")
    requested = st.button("여행 일정 생성", key="generate_schedule", disabled=not can_generate)
    if (requested or st.session_state.pop("schedule_anchor_pending", False)) and can_generate:
        with st.spinner("장소 사이의 경로와 방문 가능한 시간을 확인하고 있습니다…"):
            result = generate_trip_schedule(
                trip, selected, candidates, read_settings(), preferred,
                None if (undecided or recommended_confirmed) else (resolved_query or None),
                accommodation_undecided=undecided,
                accommodation_point=accommodation_point if confirmed else None)
        st.session_state.schedule_result = result
        st.session_state.schedule_signature = signature
        st.session_state.trip_schedule = result.schedule
        _clear_replan_state()
    result = st.session_state.get("schedule_result")
    if result is None:
        return
    for notice in result.notices:
        # Avoid duplicating return-status warnings that render_return_journey shows.
        if any(token in notice for token in ("왕복 일정", "귀가 교통편을 확인하지 못했습니다", "귀가 교통편 후보")):
            continue
        # Shown once in the calculation-guide expander / lodging header.
        if "체류시간은 기본 추정값" in notice or "임시 기준점" in notice:
            continue
        st.info(notice)
    schedule = st.session_state.get("trip_schedule")
    if schedule is None or schedule.validation_status != "VALIDATED":
        return
    from services.replan_ui import (
        resolve_progress_locks, combine_current_datetime, VisitProgressStatus)
    from models.transport import KST
    pending = st.session_state.get("pending_replan")
    if pending and pending.get("completed_keys") is not None:
        completed_keys = frozenset(pending["completed_keys"])
    else:
        progress_key = st.session_state.get("replan_progress_select")
        if progress_key is None:
            progress_key = st.session_state.get("replan_progress_key")
        if progress_key == "":
            progress_key = None
        # Migrate legacy through_key once
        if progress_key is None and st.session_state.get("replan_through_key"):
            progress_key = st.session_state.get("replan_through_key")
            st.session_state.replan_visit_status = VisitProgressStatus.COMPLETED.value
        status = st.session_state.get("replan_visit_status_radio") or st.session_state.get(
            "replan_visit_status") or VisitProgressStatus.NOT_STARTED.value
        st.session_state.replan_progress_key = progress_key
        st.session_state.replan_visit_status = status
        completed_keys, _ = resolve_progress_locks(
            schedule, progress_key=progress_key, visit_status=status)
    cur_date = st.session_state.get("replan_current_date") or schedule.trip_start_datetime.date()
    cur_time = st.session_state.get("replan_current_time") or schedule.trip_start_datetime.timetz().replace(tzinfo=None)
    try:
        current_dt = combine_current_datetime(cur_date, cur_time, KST)
    except Exception:
        current_dt = schedule.trip_start_datetime
    from services.schedule_visualization import render_schedule_visualization
    render_schedule_visualization(
        schedule, trip=trip, selected=selected, candidates=candidates,
        render_raw_items=render_schedule_items,
        render_return=render_return_journey,
        completed_keys=completed_keys,
        current_datetime=current_dt,
    )
    render_replan_panel(
        schedule, trip=trip, selected=selected, lodging_key=lodging_key,
        preferred=preferred, completed_keys=completed_keys)


def _run_replan_pipeline(
        *,
        user_text: str,
        schedule,
        trip,
        selected,
        completed_keys: frozenset,
        current_dt,
        current_place_id: str | None,
):
    """Parse (+ optional confirm already done) → engine. Never mutates active schedule."""
    from providers.http_client import HttpClient
    from providers.kakao_provider import KakaoProvider
    from providers.kakao_transit_provider import KakaoTransitProvider
    from providers.groq_provider import GroqProvider
    from services.replan_nl_parser import parse_replan_request, apply_parsed_replan
    from services.replan_ui import (
        schedule_content_signature, compute_replan_diff, unsupported_user_message)

    settings = read_settings()
    http = HttpClient(max_attempts=2)
    resolve_route = None
    place_pool = ()
    parse = None
    applied = None
    try:
        kakao = KakaoProvider(settings.kakao_rest_api_key, http) if settings.kakao_rest_api_key else None
        groq = GroqProvider(settings.groq_api_key, http, model=settings.groq_model) if settings.groq_api_key else None
        if settings.kakao_rest_api_key:
            transit = KakaoTransitProvider(settings.kakao_rest_api_key, http)

            def resolve_route(origin, dest):
                try:
                    return transit.fastest_route(origin, dest)
                except Exception:
                    return None

        main = st.session_state.get("place_result")
        nearby = st.session_state.get("nearby_result")
        pool = []
        if main:
            pool.extend(main.candidates)
        if nearby:
            pool.extend(nearby.candidates)
        seen = set()
        uniq = []
        for c in pool:
            if c.place_id in seen:
                continue
            seen.add(c.place_id)
            uniq.append(c)
        place_pool = tuple(uniq)

        parse = parse_replan_request(
            user_text,
            schedule=schedule,
            current_datetime=current_dt,
            completed_item_ids=completed_keys,
            current_place_id=current_place_id,
            settings=settings,
            groq=groq,
            kakao=kakao,
        )

        unsupported = unsupported_user_message(parse)
        if unsupported and (not parse.events or all(
                (getattr(i, "event_type", None) == "UNSUPPORTED" or not i.supported)
                for i in parse.intents)):
            return {"kind": "unsupported", "message": unsupported, "parse": parse}

        if parse.requires_confirmation and not parse.events:
            return {"kind": "confirm", "parse": parse}

        applied = apply_parsed_replan(
            parse, trip=trip, schedule=schedule, selected=selected,
            current_datetime=current_dt, completed_item_ids=completed_keys,
            settings=settings, place_pool=place_pool, resolve_route=resolve_route)
    finally:
        http.close()

    if parse is None:
        return {"kind": "failed", "message": "일정을 다시 계산하지 못했습니다. 기존 일정은 그대로 유지됩니다.",
                "parse": None}

    if applied is None:
        return {"kind": "failed", "message": "일정을 다시 계산하지 못했습니다. 기존 일정은 그대로 유지됩니다.",
                "parse": parse}

    if applied.skipped or applied.replan is None:
        msg = (applied.notices[0] if applied.notices else
               "일정을 다시 계산하지 못했습니다. 기존 일정은 그대로 유지됩니다.")
        return {"kind": "failed", "message": msg, "parse": parse, "applied": applied}

    replan = applied.replan
    from models.replan import ReplanStatus
    if replan.status in (ReplanStatus.REPLAN_FAILED, ReplanStatus.REPLAN_INFEASIBLE):
        return {"kind": "preview", "parse": parse, "replan": replan, "applied": applied,
                "base_signature": schedule_content_signature(schedule)}

    proposed = replan.proposed_schedule or schedule
    diff = compute_replan_diff(
        schedule, proposed, completed_ids=completed_keys,
        events=applied.applied_events or parse.events, replan=replan)
    return {
        "kind": "preview",
        "parse": parse,
        "replan": replan,
        "applied": applied,
        "diff": diff,
        "base_signature": schedule_content_signature(schedule),
        "user_text": user_text,
    }


def render_replan_panel(schedule, *, trip, selected, lodging_key, preferred, completed_keys) -> None:
    """Phase 6.3 — 여행 중 일정 변경 (Preview / Apply / Cancel)."""
    from models.transport import KST
    from services.replan_ui import (
        visit_choices, resolve_progress_locks, combine_current_datetime,
        inferred_current_place_name, schedule_content_signature,
        trip_replan_signature, status_user_message, confirmation_candidates,
        lodging_label, return_preview_lines, finalize_parse_with_place,
        compute_replan_diff, event_request_lines, diff_kind_label,
        can_apply_pending, apply_pending_to_active, VisitProgressStatus,
        progress_time_consistency_warning, find_visit_by_key,
    )
    from services.replan_nl_parser import apply_parsed_replan

    st.subheader("여행 중 일정 변경")
    if schedule is None or schedule.validation_status != "VALIDATED":
        st.info("먼저 여행 일정을 생성해주세요.")
        return

    choices = visit_choices(schedule)
    choice_map = {k: label for k, label in choices}
    options = [""] + [k for k, _ in choices]
    progress_key = st.session_state.get("replan_progress_key") or ""
    if progress_key not in options:
        progress_key = ""
    status_value = st.session_state.get("replan_visit_status") or VisitProgressStatus.NOT_STARTED.value
    pending_freeze = st.session_state.get("pending_replan") is not None

    st.markdown("**현재 진행 상태**")
    selected_progress = st.selectbox(
        "기준 장소",
        options,
        index=options.index(progress_key) if progress_key in options else 0,
        format_func=lambda key: "선택 안 함" if not key else choice_map.get(key, key),
        key="replan_progress_select",
        disabled=pending_freeze,
    )
    status_labels = {
        VisitProgressStatus.NOT_STARTED.value: "방문 전",
        VisitProgressStatus.IN_PROGRESS.value: "방문 중",
        VisitProgressStatus.COMPLETED.value: "방문 완료",
    }
    status_options = list(status_labels)
    status_index = status_options.index(status_value) if status_value in status_options else 0
    selected_status = st.radio(
        "이 장소의 상태",
        status_options,
        index=status_index,
        format_func=lambda v: status_labels[v],
        horizontal=True,
        key="replan_visit_status_radio",
        disabled=pending_freeze or not selected_progress,
    )
    if pending_freeze:
        st.caption("변경안 검토 중에는 진행 상태를 바꿀 수 없습니다. 취소 후 다시 지정해주세요.")
        completed_keys = frozenset(
            st.session_state.get("pending_replan", {}).get("completed_keys") or completed_keys)
        current_place_id = st.session_state.get("pending_replan", {}).get("current_place_id")
    else:
        st.session_state.replan_progress_key = selected_progress or None
        st.session_state.replan_visit_status = (
            selected_status if selected_progress else VisitProgressStatus.NOT_STARTED.value)
        completed_keys, current_place_id = resolve_progress_locks(
            schedule,
            progress_key=st.session_state.replan_progress_key,
            visit_status=st.session_state.replan_visit_status,
        )
        # Keep legacy key cleared so old path cannot re-lock
        st.session_state.pop("replan_through_key", None)

    if selected_progress and not pending_freeze:
        visit = find_visit_by_key(schedule, selected_progress)
        if visit and selected_status == VisitProgressStatus.COMPLETED.value:
            st.caption(f"마지막으로 완료한 일정: {visit.place_name}까지")
        elif visit and selected_status == VisitProgressStatus.IN_PROGRESS.value:
            st.caption(f"현재 방문 중: {visit.place_name} (아직 완료 아님)")
        elif visit:
            st.caption(f"다음에 방문할 장소: {visit.place_name} (방문 전)")

    # Current datetime (demo-adjustable; not forced system now)
    default_date = st.session_state.get("replan_current_date")
    default_time = st.session_state.get("replan_current_time")
    if default_date is None:
        default_date = schedule.trip_start_datetime.date()
        # Prefer last completed visit end, else trip start
        if completed_keys:
            from services.replan_validate import _flat_items
            from models.replan import item_key
            for item in _flat_items(schedule):
                if item_key(item) in completed_keys:
                    default_date = item.end_datetime.date()
                    default_time = item.end_datetime.timetz().replace(tzinfo=None)
        st.session_state.replan_current_date = default_date
        if default_time is not None:
            st.session_state.replan_current_time = default_time
    if st.session_state.get("replan_current_time") is None:
        st.session_state.replan_current_time = schedule.trip_start_datetime.timetz().replace(tzinfo=None)

    col_d, col_t = st.columns(2)
    with col_d:
        cur_date = st.date_input(
            "설정한 현재 날짜",
            value=st.session_state.replan_current_date,
            key="replan_date_input",
        )
    with col_t:
        cur_time = st.time_input(
            "설정한 현재 시간",
            value=st.session_state.replan_current_time,
            key="replan_time_input",
        )
    st.session_state.replan_current_date = cur_date
    st.session_state.replan_current_time = cur_time
    try:
        current_dt = combine_current_datetime(cur_date, cur_time, KST)
    except Exception:
        current_dt = schedule.trip_start_datetime

    warn = progress_time_consistency_warning(
        schedule,
        progress_key=st.session_state.get("replan_progress_key"),
        visit_status=st.session_state.get("replan_visit_status"),
        current_datetime=current_dt,
    )
    if warn:
        st.warning(warn)

    # Prefer explicit progress selection for "여기" / location caption
    if current_place_id:
        visit = find_visit_by_key(schedule, st.session_state.get("replan_progress_key"))
        status = st.session_state.get("replan_visit_status")
        if visit and status == VisitProgressStatus.COMPLETED.value:
            st.caption(f"현재 위치: 방문 완료로 지정 · {visit.place_name}")
        elif visit and status == VisitProgressStatus.IN_PROGRESS.value:
            st.caption(f"현재 위치: 방문 중 · {visit.place_name}")
        elif visit:
            st.caption(f"현재 위치: 방문 전(예정) · {visit.place_name}")
        else:
            st.caption(f"현재 위치: 일정 기준 추정 · {inferred_current_place_name(schedule, current_dt, completed_keys)}")
    else:
        place_name = inferred_current_place_name(schedule, current_dt, completed_keys)
        st.caption(f"현재 위치: 일정 기준 추정 · {place_name}")
    st.caption(f"숙소/기준점: {lodging_label(schedule)}")

    # Confirmation flow
    pending_confirm = st.session_state.get("pending_confirmation")
    if pending_confirm is not None:
        parse = pending_confirm.get("parse")
        st.warning("어떤 장소를 대상으로 할까요?")
        for notice in (parse.notices if parse else ()):
            st.caption(notice)
        cands = confirmation_candidates(parse, schedule)
        if not cands:
            st.info("선택할 장소가 없습니다. 요청을 다시 입력해주세요.")
            if st.button("확인 취소", key="replan_confirm_cancel"):
                st.session_state.pop("pending_confirmation", None)
                st.rerun()
        else:
            labels = {pid: name for pid, name in cands}
            pick = st.radio(
                "장소 선택",
                list(labels),
                format_func=lambda pid: labels[pid],
                key="replan_confirm_choice",
            )
            c1, c2 = st.columns(2)
            if c1.button("이 장소로 일정 다시 계산", key="replan_confirm_ok", type="primary"):
                finalized = finalize_parse_with_place(parse, pick, occurred_at=current_dt)
                st.session_state.pop("pending_confirmation", None)
                with st.spinner("남은 일정을 다시 계산하고 있습니다..."):
                    applied = apply_parsed_replan(
                        finalized, trip=trip, schedule=schedule, selected=selected,
                        current_datetime=current_dt, completed_item_ids=completed_keys,
                        settings=read_settings())
                if applied.skipped or applied.replan is None:
                    st.error(applied.notices[0] if applied.notices else
                             "일정을 다시 계산하지 못했습니다. 기존 일정은 그대로 유지됩니다.")
                else:
                    replan = applied.replan
                    proposed = replan.proposed_schedule or schedule
                    diff = compute_replan_diff(
                        schedule, proposed, completed_ids=completed_keys,
                        events=applied.applied_events or finalized.events, replan=replan)
                    import uuid
                    st.session_state.pending_replan = {
                        "id": str(uuid.uuid4()),
                        "user_text": finalized.user_text,
                        "parse": finalized,
                        "replan": replan,
                        "diff": diff,
                        "completed_keys": list(completed_keys),
                        "current_place_id": current_place_id,
                        "base_signature": schedule_content_signature(schedule),
                        "trip_signature": trip_replan_signature(
                            trip, selected, preferred_place_id=preferred,
                            lodging_key=lodging_key, schedule=schedule),
                    }
                    st.rerun()
            if c2.button("취소", key="replan_confirm_abort"):
                st.session_state.pop("pending_confirmation", None)
                st.rerun()
        return

    user_text = st.text_area(
        "현재 상황이나 바꾸고 싶은 내용을 입력하세요.",
        value=st.session_state.get("replan_user_text") or "",
        placeholder="예: 여기 문 닫았어 · 시장은 빼줘 · 30분 늦었어 · 너무 피곤해",
        key="replan_input_area",
        height=80,
    )
    st.caption("예: 여기 문 닫았어 · 시장은 빼줘 · 30분 늦었어 · 너무 피곤해")

    if st.button("일정 다시 계산", key="replan_compute", type="primary"):
        text = (user_text or "").strip()
        st.session_state.replan_user_text = text
        if not text:
            st.warning("변경할 내용을 입력해주세요.")
        else:
            # Drop old preview before new compute
            st.session_state.pop("pending_replan", None)
            with st.spinner("변경 요청을 확인하고 있습니다..."):
                outcome = _run_replan_pipeline(
                    user_text=text, schedule=schedule, trip=trip, selected=selected,
                    completed_keys=completed_keys, current_dt=current_dt,
                    current_place_id=current_place_id)
            if outcome["kind"] == "unsupported":
                st.warning(outcome["message"])
            elif outcome["kind"] == "confirm":
                st.session_state.pending_confirmation = {
                    "parse": outcome["parse"],
                    "user_text": text,
                }
                st.rerun()
            elif outcome["kind"] == "failed":
                st.error(outcome["message"])
            else:
                import uuid
                with st.spinner("남은 일정을 다시 계산하고 있습니다..."):
                    pass  # already computed in pipeline
                st.session_state.pending_replan = {
                    "id": str(uuid.uuid4()),
                    "user_text": text,
                    "parse": outcome["parse"],
                    "replan": outcome["replan"],
                    "diff": outcome.get("diff"),
                    "completed_keys": list(completed_keys),
                    "current_place_id": current_place_id,
                    "base_signature": outcome["base_signature"],
                    "trip_signature": trip_replan_signature(
                        trip, selected, preferred_place_id=preferred,
                        lodging_key=lodging_key, schedule=schedule),
                }
                st.rerun()

    pending = st.session_state.get("pending_replan")
    if pending is None:
        return

    replan = pending["replan"]
    parse = pending.get("parse")
    diff = pending.get("diff")
    if diff is None and replan.proposed_schedule is not None:
        diff = compute_replan_diff(
            schedule, replan.proposed_schedule, completed_ids=completed_keys,
            events=getattr(parse, "events", ()) or (), replan=replan)
        pending["diff"] = diff

    st.markdown("### 일정 변경안")
    st.write(f'요청: "{pending.get("user_text") or ""}"')
    from models.replan import ReplanStatus as _RS
    is_no_change = replan.status == _RS.REPLAN_NO_CHANGE
    if is_no_change:
        st.info(replan.notices[0] if replan.notices else status_user_message(replan.status))
    else:
        st.info(status_user_message(replan.status))

    if parse and parse.events:
        st.write("적용 요청:")
        for line in event_request_lines(parse.events, schedule):
            st.write(f"- {line}")
    elif parse and getattr(parse, "intents", None):
        for intent in parse.intents:
            if intent.event_type == "PLACE_CLOSED" or (
                    hasattr(intent.event_type, "value") and intent.event_type.value == "PLACE_CLOSED"):
                st.caption("사용자 입력에 따른 제외 요청입니다. 실제 영업 종료를 확인한 결과가 아닙니다.")

    if not is_no_change:
        if diff and diff.summary_lines:
            st.write("변경 사항:")
            for line in diff.summary_lines:
                st.write(f"- {line}")
        if diff and diff.items:
            removed = [d for d in diff.items if d.kind.value == "REMOVED"]
            added = [d for d in diff.items if d.kind.value == "ADDED"]
            timed = [d for d in diff.items if d.kind.value in {
                "TIME_CHANGED", "MEAL_CHANGED", "RETURN_CHANGED", "ACCOMMODATION_CHANGED"}]
            if removed:
                st.write("제외:")
                for item in removed:
                    st.write(f"- {item.place_name}"
                             + (f" ({item.detail})" if item.detail else ""))
            if added:
                st.write("추가:")
                for item in added:
                    st.write(f"- {item.place_name}"
                             + (f" ({item.detail})" if item.detail else ""))
            if timed:
                st.write("시간·기타 변경:")
                for item in timed:
                    st.write(f"- {diff_kind_label(item.kind)}: {item.place_name}"
                             + (f" ({item.detail})" if item.detail else ""))

        if replan.main_status == "moved_meal":
            st.write("- 기준 식당의 식사 시간대가 변경됩니다.")
        elif replan.main_status == "INFEASIBLE":
            st.warning("선택한 기준 장소를 현재 조건 안에 포함하기 어렵습니다.")

        proposed = replan.proposed_schedule
        if proposed is not None:
            if proposed.return_journey or proposed.final_arrival_datetime:
                for line in return_preview_lines(proposed):
                    st.write(f"- {line}")
            # FATIGUE: only claim reduction when after < before
            if parse and any(
                    (getattr(e, "event_type", None) and
                     (e.event_type.value if hasattr(e.event_type, "value") else e.event_type) == "FATIGUE")
                    for e in (parse.events or ())):
                from services.replan_validate import _flat_items
                from models.replan import item_key as _ik
                before = sum(1 for i in _flat_items(schedule) if i.item_type != "TRAVEL"
                             and _ik(i) not in completed_keys)
                after = sum(1 for i in _flat_items(proposed) if i.item_type != "TRAVEL"
                            and _ik(i) not in completed_keys)
                if after < before:
                    st.write(f"- 남은 활동 수를 줄인 변경안입니다. ({before}곳 → {after}곳)")

        for notice in replan.notices:
            if "MAIN_INFEASIBLE" in notice:
                continue
            st.caption(notice)

    a1, a2 = st.columns(2)
    apply_clicked = a1.button(
        "변경 적용", key="replan_apply", type="primary", disabled=is_no_change)
    cancel_clicked = a2.button("취소" if not is_no_change else "닫기", key="replan_cancel")

    if cancel_clicked:
        st.session_state.pop("pending_replan", None)
        st.rerun()

    if apply_clicked:
        ok, message = can_apply_pending(
            pending, active_schedule=schedule, trip=trip, selected=selected,
            preferred_place_id=preferred, lodging_key=lodging_key,
            already_applied_id=st.session_state.get("replan_applied_id"))
        if not ok:
            st.error(message)
            st.session_state.pop("pending_replan", None)
            return
        st.session_state.replan_applied_id = pending.get("id")
        st.session_state.trip_schedule = apply_pending_to_active(pending, schedule)
        result = st.session_state.get("schedule_result")
        if result is not None and hasattr(result, "model_copy"):
            try:
                st.session_state.schedule_result = result.model_copy(
                    update={"schedule": st.session_state.trip_schedule})
            except Exception:
                pass
        st.session_state.pop("pending_replan", None)
        st.session_state.pop("pending_confirmation", None)
        st.success("변경한 일정을 적용했습니다.")
        st.rerun()


def render_diagnostics(settings: Settings) -> None:
    if not settings.enable_api_diagnostics:
        return
    with st.expander("개발용 API 연결 점검"):
        st.caption("버튼을 눌렀을 때만 호출합니다. 조회 쿼터를 사용하며 Groq 추론은 실행하지 않습니다.")
        if st.button("API 연결상태 확인"):
            with st.spinner("외부 API 연결 확인 중…"):
                st.session_state.health_results = check_connections(settings)
        for result in st.session_state.get("health_results", []):
            st.write(f"**{result.provider} · {result.status}**")
            st.caption(f"{result.detail} · {result.latency_ms}ms · {result.checked_at}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    st.set_page_config(page_title="여행 다시짜기", page_icon="🧳", layout="centered")
    st.title("여행 다시짜기")
    st.caption("출발부터 귀가까지, 상황이 바뀌면 남은 일정만 다시 계산합니다.")
    settings = read_settings()
    render_trip_form()
    render_diagnostics(settings)


if __name__ == "__main__":
    main()
