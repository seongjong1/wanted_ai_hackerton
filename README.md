# AI 여행 플래너 — Phase 3

국내 여행 조건을 검증한 뒤 실제 TAGO 장거리 교통편을 조회·비교하는 Python/Streamlit 앱입니다.
`TripRequest → 지역/거점 확인 → 실 API 운행 조회 → 접근 경로·승차 버퍼 검증 → 정렬 → 선택`을 구현했습니다.
열차(KTX/일반철도), 고속버스, 시외버스를 대상으로 합니다. 교통편 선택 후 도착 거점 주변의 실제 장소 후보를 검색합니다.
전체 일정·방문 순서·지도·재계획은 아직 실행하지 않습니다.

## 실행

Python 3.12 기준, 프로젝트 루트에서 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m streamlit run app.py
```

macOS/Linux는 `.venv/bin/python`을 사용합니다. 운영 설치는 `requirements.txt`만 사용합니다.
Secret이 없어도 입력 화면과 검증은 작동합니다. 앱은 상태를 세션 메모리에만 보관하며 새 세션/서버 재시작 시 사라질 수 있습니다.

## Secret 설정

`.streamlit/secrets.toml.example`을 `.streamlit/secrets.toml`로 복사하여 **로컬에서** 값을 입력합니다.

- `DATA_GO_KR_API_KEY`: 공공데이터포털 인증키. 디코딩 키 권장, 인코딩 키도 한 번 디코딩한 뒤 requests로 인코딩합니다.
- `KAKAO_REST_API_KEY`: Kakao REST API 키.
- `GROQ_API_KEY`: Groq 키.
- `ENABLE_API_DIAGNOSTICS = true`: 개발용 연결 점검 UI 활성화. 기본값은 false이며 공개 배포에서는 끕니다. 인증 기능을 대신하는 설정은 아닙니다.

환경 변수도 지원하며 비어 있지 않은 환경 변수가 Secret보다 우선합니다.
모델은 `openai/gpt-oss-120b`로 고정합니다. 실제 키·원문 요청·알레르기 등 개인정보를 로그로 남기지 않습니다.
실제 `secrets.toml`, `.env`, 가상환경, 임시 파일은 `.gitignore`로 제외됩니다.

## 파일별 역할

```text
app.py                             기존 입력 폼, 교통 후보 카드/선택, 세션 상태, 개발용 점검 UI
config.py                          Secret/환경 변수 로딩, 키 repr 제외
models/__init__.py                 모델 패키지
models/trip_request.py             Pydantic TripRequest, 성향 enum, 반경 및 날짜/시간 검증
models/transport.py                후보/거점/매칭/조회 결과/교통수단별 상태 모델
models/access.py                   접근 경로·구간·좌표·승차 가능성·총 소요시간 모델
models/place.py                    실제 장소 후보와 조건 확인 상태, 장소 검색 결과
providers/__init__.py              외부 API 패키지
providers/http_client.py           timeout, 안전한 오류, 제한적 재시도, 지연 로그
providers/kakao_provider.py        키워드·좌표·반경 장소 검색과 연결 점검
providers/kakao_transit_provider.py Kakao 대중교통 경로 조회, 최단 totalTime 경로 선택
providers/tago_base.py             TAGO 응답 검증, 목록 정규화, 제한된 페이지네이션
providers/tago_train_provider.py   열차 도시코드·역·운행 조회와 기존 연결 점검
providers/tago_bus_provider.py     고속/시외 도시코드·터미널·운행 조회와 기존 연결 점검
providers/groq_provider.py         모델 목록 점검, schema 검증을 포함한 JSON 추론 어댑터
services/__init__.py               서비스 패키지
services/health_service.py         API별 점검 결과와 오류 안내, 개별 실패 격리
services/location_service.py       Kakao 지역 확인, TAGO 거점 이름 매칭, 요청 내 역 목록 재사용
services/transport_service.py      API 결과 변환, 시간 필터, 중복 제거, 정렬, 수단별 실패 격리
services/access_service.py         정확한 장소 좌표화, 접근시간 검증, 요청 내 캐시
services/place_service.py          반경 검색, 성향 정렬, AI 추천 근거 검증
tests/test_trip_request.py         정상·경계·입력 오류 검증
tests/test_providers.py            HTTP/API/LLM 오류, 재시도, 키 미노출, 서비스 테스트
tests/test_app.py                  Streamlit AppTest 폼/세션/연결 점검 테스트
tests/test_transport.py            시간/요금/중복/정렬/부분 실패/결과 제한 테스트
tests/test_locations.py            자연어 지역/거점 매칭 및 모호한 지역 테스트
tests/test_transport_live.py       명시적으로 활성화하는 실제 API smoke 테스트
tests/test_access.py               버퍼·총 소요시간·자정·경계 시각 테스트
tests/test_access_service.py       접근 필터·거점 매칭·캐시·부분 실패 테스트
tests/test_kakao_transit.py         경로 API 계약·초 단위·NO_RESULTS·잘못된 응답 테스트
tests/test_places.py               장소 모델·반경·정렬·실패·조건·AI 근거 검증
tests/test_places_ui.py            장소 조회/세션 재사용/교통 선택 변경 테스트
tests/test_places_live.py          별도 활성화하는 실제 장소 API 통합 테스트
.streamlit/secrets.toml.example    비어 있는 Secret 예시
.gitignore                        민감정보·로컬 생성물 제외
requirements.txt                  운영 의존성
requirements-dev.txt              테스트 의존성
pytest.ini                        tests 디렉터리만 수집
README.md                         실행·설정·설계·제약·다음 단계
```

## 구조와 범위

입력은 `app.py → TripRequest → TransportService → Provider → HttpClient`로 흐릅니다.
조회 결과를 검증·정렬한 뒤 `transport_result`, `transport_candidates`, `selected_transport`에 저장합니다.
`selected_transport`는 최초 조회 시 None이며 카드의 선택 버튼으로 후보 모델을 저장합니다.
화면 재실행·선택 시 재조회하지 않고, 폼을 새로 제출하면 이전 결과와 선택을 제거합니다.
연결 점검은 `app.py → health_service → Provider → HttpClient → 외부 API`로 흐릅니다.
UI에는 HTTP 호출 코드가 없고, Provider는 Streamlit에 의존하지 않습니다.
별도 FastAPI 서버·DB·Agent Loop·미사용 engine/service 파일은 추가하지 않았습니다.
국외 확장 시 Provider와 국가별 요청 검증을 추가할 수 있으며 현재 UI는 국내 여행만 대상으로 합니다.
Kakao 장소 검색으로 출발지/거점 좌표를 확인하고 대중교통 경로 API로 접근 이동을 계산합니다.

검증 항목은 공백 출발지/여행지, 동일 이름(공백/대소문자 정규화), 역전된 날짜,
당일 종료시간 ≤ 출발시간, 성향 미선택/허용되지 않은 성향, 잘못된 반경입니다.
서로 다른 별칭이 같은 장소를 가리키는지는 다음 단계의 API 좌표 확인에서 검증합니다.
‘500m 이상’은 독립 옵션으로 보존하며 임의로 1km 등에 매핑하지 않습니다.
숙박 여부와 여행 일수는 독립 조건입니다. 알레르기는 쉼표 단위로 정리하고 중복을 제거합니다.

## 연결 점검과 실패 처리

연결 점검은 사용자가 버튼을 누를 때만 실행합니다. 고속버스와 시외버스는 별도로 확인합니다.
Groq는 인증 및 지정 모델 존재만 확인하며 추론 동작/남은 쿼터를 보증하지 않습니다.
한 제공기관이 실패해도 나머지는 점검하며, 실패를 정상이나 샘플 데이터로 대체하지 않습니다.

- 연결/읽기 timeout: 3초/8초. 일반 GET은 최대 3회 시도, UI 점검은 최대 2회.
- timeout, connection error, HTTP 429/500/502/503/504에 지수 backoff + jitter.
- 401/403/잘못된 요청/리디렉션은 재시도하지 않습니다. 인증키 유출 방지를 위해 리디렉션을 따르지 않습니다.
- 숫자 `Retry-After`를 반영하되 8초를 넘으면 즉시 실패로 반환합니다. HTTP 날짜 형식은 기본 backoff를 사용합니다.
- TAGO HTTP 200 내부 `resultCode`도 검사합니다. 일일 한도 및 인증 오류는 반복하지 않습니다.
- JSON 구조/파싱 실패를 안전한 ProviderError로 변환합니다. XML 오류 응답은 `invalid_json`으로 안내합니다.
- Groq POST는 중복 비용을 피하기 위해 자동 재시도하지 않습니다. 구조화 응답을 Pydantic으로 검증합니다.
- 로그는 provider, attempt, 성공/오류 코드, latency만 기록합니다. URL/헤더/본문/예외 원문은 기록하지 않습니다.

점검은 순차적으로 실행되어 장애 시 여러 API의 대기시간이 누적될 수 있습니다.
현재 timeout은 개별 연결/읽기 제한이며 전체 작업의 절대 시간 제한은 아닙니다.

## 테스트

```powershell
.\.venv\Scripts\python -m pytest -q
```

테스트는 실제 키/네트워크 없이 모의 응답으로 실행됩니다.
실제 키를 설정한 뒤 개발용 연결 점검 버튼으로 현재 배포 환경의 인증과 API 응답을 별도 확인하세요.
모의 테스트 통과는 실제 서비스의 가용성이나 교통 데이터 정확성을 보증하지 않습니다.

### Phase 1 실행 기록

- Python 3.12.10 가상환경에서 `python -m pytest -q -p no:cacheprovider`: **50 passed (1.22초)**.
- Streamlit 서버 실행 및 `/_stcore/health`: **HTTP 200, ok**.
- 가상환경 의존성 설치 버전/요구사항 검사: **OK**.
- 실제 Secret은 제공되지 않아 실 API 인증 호출 및 Groq 추론은 실행하지 않았습니다.
- 이 Codex Windows 호스트에서는 Python 임시 폴더(mode 0700)의 쓰기/정리 권한 오류가 발생했습니다.
  설치 시에만 작업용 스크립트로 상속 ACL을 사용해 해결했으며 제품 코드에는 우회 코드를 넣지 않았습니다.
  테스트는 종료 코드 0으로 통과했지만, 종료 시 Streamlit의 임시 폴더 정리에서 같은 권한 오류가 출력되었습니다.
- 초기 테스트의 모의 Response 종료 처리 설정 누락을 수정했고, `pytest.ini`로 임시 폴더 수집을 방지했습니다.
- 기존 저장소가 없어 회귀 비교 대상은 없으며, 세션 유지/잘못된 재제출 시 이전 결과 제거는 AppTest로 검증했습니다.

### Phase 2 실행 결과

- `python -m pytest -q -p no:cacheprovider`: **93 passed, 1 skipped (1.53초)**, 종료 코드 0.
- 기존 입력 모델·Health Check·HTTP 오류·Groq 테스트도 함께 실행했습니다.
- 새 검증: duration, 자정 경계, 희망 출발시간/종료시간 필터, 도착 우선 정렬,
  소요시간/요금 tie-break, 빈 응답, 일부 API 실패, 중복 제거, 잘못된 필수 필드,
  미확인 요금, 결과 제한, 지명 매칭, 페이지 누락/반복 방지, 카드 선택과 세션 재사용.
- 실제 API 테스트는 기본 실행에서 건너뛰고 별도 활성화해 **1 passed (0.77초)**를 확인했습니다.
- Secret 값이 예시 파일에 들어 있어 초기에는 앱이 읽지 못했습니다. 실제 값을 Git 제외 대상인
  `.streamlit/secrets.toml`로 옮기고 예시 파일은 빈 값으로 복구했습니다. 배포 ZIP에는 실제 키를 넣지 않습니다.
- 실제 서울→부산, 2026-09-12 09:00~23:59 조회에서 열차 60개, 고속버스 28개,
  시외버스 4개가 시간 조건을 통과했습니다. 화면용으로 각각 5/5/4개, 전체 추천 5개를 보관했습니다.
  필수 데이터가 잘못된 고속버스 행 1개를 제외했고, 시외버스의 거점 탐색 상한을 ‘일부 결과’로 알렸습니다.
- 서울역·강남·경북 구미·구미·전주 입력을 실제 Kakao/TAGO 데이터로 철도 거점에 연결했습니다.
  이는 확인한 구간과 조회 시점의 결과이며 모든 날짜/지역의 가용성을 보증하지 않습니다.
- Phase 1과 동일한 Windows 임시 폴더 정리 권한 오류가 테스트 종료 시 출력됩니다.
  테스트 assertion 실패는 없으며 제품 코드에 환경 전용 우회 로직을 추가하지 않았습니다.

실 API 점검은 환경 변수 `DATA_GO_KR_API_KEY`와 `RUN_LIVE_TRANSPORT_TESTS=1`을 설정한 뒤
`python -m pytest tests/test_transport_live.py -q`로 실행합니다. 실제 조회 쿼터를 사용합니다.

## Phase 2 변경 기록

새 파일은 `models/transport.py`, `services/location_service.py`, `services/transport_service.py`,
`tests/test_transport.py`, `tests/test_locations.py`, `tests/test_transport_live.py`입니다.
수정 파일은 `app.py`, `providers/tago_base.py`, `providers/tago_train_provider.py`,
`providers/tago_bus_provider.py`, `tests/test_app.py`, `tests/test_providers.py`, `README.md`입니다.
추가로 `.streamlit/secrets.toml.example`의 실제 값을 제거하고 로컬 전용 `secrets.toml`로 옮겼습니다.
기존 Provider에 조회 메서드만 확장하고, 지역 매칭/교통 비즈니스 로직을 서비스로 분리하기 위한 변경입니다.
`TripRequest`, Config/Secret 처리, Kakao/Groq Provider, Health Service, 의존성은 변경하지 않았습니다.

### TransportCandidate

`transport_type`, `departure_place`, `arrival_place`, 한국 시간대가 있는 datetime 형태의
`departure_time`/`arrival_time`, 계산 속성 `duration_minutes`, Decimal 또는 None인 `price`,
`grade`, `route_id`, `train_number`, `provider`, `raw_data`를 보관합니다.
소요시간은 도착 datetime에서 출발 datetime을 뺀 값입니다. 날짜 없는 HH:mm은 추정하지 않고 제외합니다.
열차 응답의 14자리, 버스 응답의 12자리 날짜·시각을 지원합니다.
`raw_data`는 API 원본을 보관하지만 repr/기본 직렬화와 UI에는 포함하지 않습니다.
요금이 없거나 잘못되면 None으로 표시하며 무료 요금으로 바꾸지 않습니다.

### 지명 매칭

1. Kakao 검색 결과의 주소에서 시·도/도시를 확인합니다. 서로 다른 지역이 섞이면 재입력을 안내합니다.
2. TAGO 도시 목록에서 실제 도시코드를 얻고, 열차는 해당 시·도의 역 목록을 조회합니다.
3. 역/터미널 이름이 일치하면 실제 API ID를 사용합니다. 공백·‘역’/‘터미널’ 등 접미사만 정규화합니다.
4. 도시/동네 입력은 지역명과 일치하는 실제 역/터미널 후보를 탐색합니다.
   예를 들어 Kakao가 강남을 서울로 확인하면 서울 이름의 터미널을 탐색합니다.
5. 복수 거점은 이름/ID 순으로 정리해 출발/도착 각각 최대 3개를 조회하고 범위 제한을 알립니다.
6. 매칭이 불가능하면 ‘매칭 실패’를 반환합니다. 역 ID나 터미널 ID를 하드코딩하거나 생성하지 않습니다.

Kakao 장애 시 TAGO에서 명확하게 확인할 수 있는 지역/이름만 사용합니다.
지역명이 없는 비도시 역명은 Kakao 확인이 필요할 수 있습니다.
터미널의 통칭과 TAGO 명칭이 다른 경우 자동으로 동일 장소라고 가정하지 않습니다.
이름만으로 탐색한 버스 거점은 주소 일치/근접성을 보증하지 않으므로 실제 거점 이름을 화면에 표시합니다.

### 조회·필터·정렬

교통수단별로 매칭된 출발/도착 거점의 직통 운행을 여행 시작 날짜로 조회합니다.
소요시간 ≤ 0, 날짜·시각 파싱 불가, 출발/도착 장소 누락은 제외합니다.
시작 날짜가 다른 운행, 희망 출발시간 이전 운행, 여행 종료 datetime 이후 도착도 제외합니다.
이는 당일 여행뿐 아니라 여러 날 여행에서도 전체 여행 종료시각을 넘지 않도록 하는 조건입니다.

정렬은 **도착 시각 → 이동 소요시간 → 가격** 순입니다. 요금 미상은 같은 시각/소요시간에서 뒤로 갑니다.
완전 동률은 수단/장소/등급/열차번호/노선 ID 순서로 결정합니다. LLM을 호출하지 않습니다.
같은 수단·장소·시각·등급·열차번호·노선 ID의 중복을 제거하고, 중복 요금이 다르면 API의 가장 낮은 확인 요금을 보존합니다.
각 수단 상위 5개를 보관하고 전체 추천 상위 5개를 표시합니다.

### 실패 격리와 UI

열차·고속·시외 각각 ‘정상/후보 없음/매칭 실패/조회 실패/일부 결과/날짜 제한’ 상태를 제공합니다.
한 거점 조합 조회가 실패하면 해당 수단의 추가 호출을 멈추고 먼저 확보한 후보를 보존합니다.
다른 교통수단은 계속 조회합니다. 잘못된 행도 정상 행을 없애지 않습니다.
페이지네이션은 최대 10페이지(페이지당 100개)이며 상한/반복 페이지를 오류로 알립니다.
수단당 45초가 지나면 새 거점 조합 요청을 중단합니다. 진행 중 HTTP/페이지네이션을 취소하는 절대 deadline은 아닙니다.

‘교통편 후보’에서 조회 상태·탐색한 실제 거점과 추천 카드가 표시됩니다.
카드는 수단/등급, 실제 장소, 날짜 포함 출발·도착시각, 소요시간, 성인 요금, 열차번호를 표시합니다.
별도 펼침 영역에서 수단별 후보를 보고, 통합 추천 카드에서 교통편을 선택할 수 있습니다.
선택은 세션 저장이며 예약이나 일정 생성이 아닙니다.

## 배포

GitHub 저장소에 소스를 올린 뒤 Streamlit Community Cloud에서 Python 3.12와 `app.py`를 선택합니다.
실제 키는 Cloud 앱 설정의 Secrets에 입력하고, `ENABLE_API_DIAGNOSTICS`는 false로 둡니다.
이 작업에서는 GitHub 저장소 생성·push·Cloud 배포를 수행하지 않았습니다.

## 다음 단계

1. 검증 구간을 주요 도시·여러 날짜로 확대하고 거점 통칭/주소 매칭 사례를 축적합니다.
2. 사용자가 모호한 출발 장소/거점을 직접 확정하는 UX와 날짜별 배차 검증을 확장합니다.
3. 좌표·장소 모델을 기반으로 목적지 도착 후 활동 후보와 시간·영업 조건을 검증합니다.
4. 검증된 교통편과 장소 ID만 활용하여 AI 설명과 일정 생성을 연결합니다.

현재는 직통 장거리 교통만 비교하며 전국 모든 거점 조합의 최적 경로를 보장하지 않습니다.
시외버스는 공식 제공 범위에 따라 여행 당일에만 조회합니다. 환승, 좌석/예약, 실시간 지연,
목적지 내 이동·귀가편은 미구현입니다. 좁은 활동반경과 동행/알레르기 조건은
TripRequest에 보존되지만 이번 교통 정렬에는 아직 적용하지 않습니다.

LLM schema 검증만으로 사실성이 확보되지는 않습니다. 추후 서비스는 후보 ID 참조와 시간/거리 등
업무 규칙을 별도로 검증하고, 시간·요금·주소·좌표는 API 원본에서만 가져와야 합니다.
알레르기/반려동물/아이 입장 가능 여부와 실제 이동시간은 현재 Provider가 보장하는 정보가 아닙니다.

## 확인한 공식 명세

- [Kakao Local API](https://developers.kakao.com/docs/ko/local/dev-guide)
- [TAGO 열차정보](https://www.data.go.kr/data/15098552/openapi.do)
- [TAGO 고속버스정보](https://www.data.go.kr/data/15098522/openapi.do)
- [TAGO 시외버스정보](https://www.data.go.kr/data/15098541/openapi.do)
- [Groq API](https://console.groq.com/docs/api-reference)

TAGO 공개 Swagger에서 확인한 도시 목록 경로는 세 서비스 모두 `/GetCtyCodeList`입니다.

## Phase 2.5 접근 이동·승차 검증

기존 입력 화면과 교통 카드를 유지하면서 `TransportCandidate.access`에 별도
`BoardingAssessment`를 연결했습니다. 기존 `duration_minutes`는 장거리 교통 소요시간 그대로입니다.
`AccessLeg`는 출발/도착 장소, 좌표와 Kakao 장소 ID, 이동수단, 소요시간, 거리(m),
출발/도착 datetime, 환승 횟수, 구간별 도보/버스/지하철 정보와 출처를 보관합니다.

Kakao `GET /v2/routing/publictraffic`에 `start_x`, `start_y`, `end_x`, `end_y`를 전달합니다.
기존 `KAKAO_REST_API_KEY`를 사용하며 추가 Secret/의존성은 없습니다.
정상 경로 중 `properties.totalTime`이 가장 작은 경로를 사용하고 초를 60으로 나눕니다.
잘못된 경로는 제외하며 `NO_RESULTS`, HTTP 오류, 좌표 매칭 실패는 `ACCESS_TIME_UNKNOWN`입니다.
이때 임의의 속도/직선거리로 소요시간을 생성하거나 0분으로 대체하지 않습니다.

탑승 조건은 **사용자 출발 datetime + 접근시간 + 승차 버퍼 ≤ 장거리 출발 datetime**입니다.
버스 버퍼는 요청 예시의 15분을 사용하며 철도도 MVP 정책으로 기본 15분을 사용합니다.
두 값은 `AccessService` 생성자의 `train_buffer_minutes`, `bus_buffer_minutes`로 조정할 수 있습니다.
탑승 불가/접근시간 미확인 후보는 상위 5개를 제한하기 전에 제외하므로 뒤쪽의 탑승 가능 후보가 살아남습니다.
같은 요청 내 동일 장소 검색, 동일 장소 ID 쌍 경로, 실패 결과를 재사용합니다. 전역에 개인정보를 캐시하지 않습니다.

`total_duration_minutes = 장거리 도착 - 사용자 출발`입니다.
이는 접근시간 + 승차 버퍼 + 추가 대기 + 장거리 이동이며 대기나 버퍼를 두 번 더하지 않습니다.
정렬은 도착 시각 → 총 소요시간 → 요금이며, 같은 사용자 출발 시각에서 도착 시각이 같으면 총 소요시간도 같습니다.
카드에는 접근 경로/구간, 거점 도착·승차 준비 완료 시각, 버퍼, 추가 대기, 총 소요시간이 표시됩니다.

이름과 좌표는 실제 Kakao 결과만 사용합니다. 역/터미널의 명확한 이름 일치를 요구하며,
‘서울경부버스터미널’과 ‘서울고속버스터미널(경부)’ 같은 교통시설 명칭은 시설 접미사를 정규화해 연결합니다.
동일 Kakao 장소 ID인 경우에만 접근 이동을 0분으로 처리하며 승차 버퍼는 유지합니다.
‘서울’처럼 출발점이 넓거나 동명이인 장소가 여러 개면 구체적인 장소명을 입력해야 합니다.

기존 직접 호출과 테스트 호환을 위해 `TransportService(access_service=None)`은 Phase 2 조회를 유지합니다.
실제 앱의 `search_transport`는 항상 AccessService를 주입하며 검증되지 않은 후보를 추천하지 않습니다.

### 검증 결과와 제한

- 전체 테스트 **125 passed, 1 skipped**. 기존 Phase 1/2 테스트 포함.
- 실제 서울역→서울경부 약 31.9분, 서울역→서울남부 약 38.95분 조회 성공.
- 서울역 09:00 기준 서울경부 승차 준비 완료는 09:46:54이며 09:35 출발편은 제외됩니다.
- 실제 2026-09-13 서울역→부산 통합 조회에서 고속버스 탑승 불가 2개가 제외되었고,
  화면용 열차 5개·고속버스 5개·시외버스 4개 모두 접근 검증을 통과했습니다.
- API는 출발 날짜/시각 파라미터가 없어 날짜별 배차·막차·실시간 지연을 보장하지 않습니다.
  ‘탑승 가능’은 API 예상 소요시간과 설정 버퍼에 따른 판단입니다. 이는 좌석/운행 보증이 아닙니다.
- Windows 테스트 종료 시 임시 폴더 정리 권한 오류가 출력되지만 테스트 assertion은 통과합니다.

새 파일: `models/access.py`, `providers/kakao_transit_provider.py`, `services/access_service.py`,
`tests/test_access.py`, `tests/test_access_service.py`, `tests/test_kakao_transit.py`.
수정: `models/transport.py`, `services/transport_service.py`, `services/location_service.py`(안내 문구),
`app.py`, `tests/test_app.py`, `README.md`.
TripRequest, TAGO/Kakao Local/Groq Provider, Health Check, Config/Secret 로딩 구조는 그대로 유지했습니다.

공식 경로 명세: [Kakao 대중교통 경로 조회](https://developers.kakao.com/docs/en/kakaomap/rest-api#get-public-transit-routes).

## Phase 3: 실제 주변 장소 후보

교통편을 선택한 다음 ‘주변 추천 장소’의 ‘주변 장소 검색’을 누릅니다.
기본 기준점은 선택한 교통편의 **도착 역/터미널**이며 기존 장소 좌표 매칭 로직을 재사용합니다.
TripRequest.destination이 구체적인 장소로 확인되면 기준점 선택 목록에 추가합니다.
사용자가 기준점을 변경한 뒤 검색 버튼을 눌러 조회합니다. 도착 거점을 확인하지 못한 경우
다른 장소를 기본 기준점으로 몰래 대체하지 않고 확인 필요 상태로 반환합니다.

### 추가/수정 파일

추가: `models/place.py`, `services/place_service.py`, `tests/test_places.py`,
`tests/test_places_ui.py`, `tests/test_places_live.py`.
수정: `app.py`, `config.py`, `.streamlit/secrets.toml.example`, `README.md`.
기존 교통·접근시간 모델/서비스, 모든 Provider, Health Check, 의존성은 수정하지 않았습니다.

### PlaceCandidate와 데이터 검증

`place_id`, `place_name`, `category`, `address`, `latitude`, `longitude`, `distance_meters`,
`phone`, `place_url`, `provider`, `preference_score`, `validation_status`, `raw_data`를 보관합니다.
추가로 `matched_preferences`, `pet_status`, `child_status`, `allergy_status`, `ai_reason`이 있습니다.
장소 ID·이름·유효한 좌표가 없는 행은 제외하고, 거리 계산은 검증한 좌표로 수행합니다.
`API_FIELDS_VALIDATED`는 필수 필드 검증을 의미하며 영업 여부/동행 안전성을 보증하지 않습니다.
raw_data는 repr과 기본 직렬화에서 제외하고 UI에 출력하지 않습니다.

반려동물/아이 상태는 SUPPORTED·NOT_SUPPORTED·UNKNOWN, 알레르기는
CONFIRMED·UNCONFIRMED·UNKNOWN을 표현할 수 있습니다. 현재 Kakao Local 응답에는
판단 근거가 없으므로 세 조건 모두 **UNKNOWN**입니다. Groq도 이를 변경할 수 없습니다.
요청에 동행/알레르기 조건이 있으면 확인 필요 안내를 표시합니다.

### 검색 전략·반경·거리

기존 KakaoProvider.search_places를 그대로 사용합니다. 성향마다 음식점·관광명소·쇼핑·문화 체험·카페·야경·주점·데이트·가족 나들이·전시관 키워드를 검색합니다.
키워드 검색에 기준점 좌표와 반경을 전달하고 각 키워드 최대 2페이지(페이지당 15개),
현재 최대 10개 성향으로 호출량을 제한합니다. 일부 검색 실패 시 다른 검색 결과는 유지합니다.
상한 도달은 UI에 표시하며 상한 내 후보만 비교한 순위입니다.

100~500m 옵션은 그대로 미터로 변환합니다. ‘500m 이상’은
`EXTENDED_ACTIVITY_RADIUS_METERS` 설정을 사용합니다. 기본 **2000m**, 허용 범위 501~20000m이며
환경 변수 또는 secrets.toml에서 변경할 수 있습니다. 잘못된 설정은 안전한 로그와 함께 기본값을 적용합니다.
거리 기준을 통일하기 위해 Kakao 반환 좌표로 Haversine 직선거리를 계산하고 반경 밖 후보를 제외합니다.
응답 distance가 없거나 잘못된 경우에도 임의의 거리·이동시간을 만들지 않습니다.

### 정렬·Groq

동일 place_id를 합치며 서로 다른 선택 성향의 검색에서 발견된 수 × 100을 성향 점수로 부여합니다.
이는 검색 관련성 점수이며 맛/품질이나 가족·반려동물 적합성을 검증한 점수가 아닙니다.
정렬은 **성향 점수 내림차순 → 거리 → 데이터 완전성 → 확인된 조건 충돌 → ID** 순서입니다.
조건이 UNKNOWN이면 충돌하거나 지원한다고 임의 판단하지 않습니다. UI와 세션에는 상위 15개를 저장합니다.

Groq는 상위 10개에 대해 최대 1회 호출합니다. 실제 후보의 ID·카테고리·관련 성향·거리만 전달하며
장소명/주소나 조건 안전성을 새로 생성하게 하지 않습니다. 응답 schema는 ID·관련 성향·근거 코드입니다.
ID의 후보 포함 여부, 중복 ID, 실제 관련 성향, 거리(200m 이내), 복수 성향 조건을 모두 검증합니다.
근거 코드에 해당하는 짧은 한국어 이유를 검증된 데이터로 렌더링합니다. 자유 문장의 환각을 노출하지 않습니다.
실패/허위 ID/근거 없는 판단은 전체 AI 응답을 폐기하고 기본 후보·정렬을 그대로 유지합니다.

### UI·테스트·한계

장소명·카테고리·주소·거리·전화·Kakao 장소 링크와 확인 상태를 카드로 표시합니다.
조회 결과는 place_result/place_candidates에 보관하고 단순 rerun에서는 API를 호출하지 않습니다.
교통편 재선택 또는 여행 조건 재제출 시 이전 장소 결과와 기준점 선택을 제거합니다.

- 기본 테스트: **148 passed, 2 skipped (1.94초)**, 기존 Phase 1~2.5 회귀 테스트 포함.
- 실제 장소 통합 테스트 별도 활성화: **1 passed (2.81초)**.
- 실제 구미역 500m 반경, 맛집/관광 조건에서 15개 후보를 반환했습니다.
  Groq 상위 10개 이유 검증 완료, 반려동물/아이/알레르기 상태는 모두 UNKNOWN을 유지했습니다.
- 검색 페이지 상한 도달은 ‘일부 결과’로 표시되었습니다.
- Windows 임시 폴더 정리 권한 오류는 테스트 종료 시 여전히 출력되지만 assertion은 모두 통과했습니다.

실 API 테스트: 환경 변수에 KAKAO_REST_API_KEY, 선택적으로 GROQ_API_KEY를 설정하고
`RUN_LIVE_PLACE_TESTS=1`로 `python -m pytest tests/test_places_live.py -q`를 실행합니다.
교통편은 기준점 식별용 테스트 fixture이며, 이 테스트는 실제 열차 운행시간을 검증하지 않습니다.

현재 한계는 검색어 기반 관련성, 제한된 검색 페이지, 직선거리, 미확인 영업시간·동행 조건입니다.
다음 단계에서는 장소의 영업/입장 조건과 실제 이동시간을 확인한 후 일정 생성에 연결해야 합니다.
이번 Phase에는 방문 순서·시간표·체류시간·지도·날씨·실시간 재계획·DB를 구현하지 않았습니다.


# Phase 2.5 UI PATCH 완료 보고

## 변경 파일
- app.py: 화면 렌더링만 변경. 기존 함수명과 선택 상태 처리 유지.
- tests/test_app.py: 제거된 Provider 표시 검증을 비노출 및 내부 데이터 보존 검증으로 변경하고 선택/장소 섹션 검증 추가.
- tests/test_ui_patch.py: 후보 0/1/5/7개에 대한 UI 회귀 테스트 4개 추가.
- README.md: 본 패치 결과 및 남은 문제 기록.

## 화면 변경
입력 조건 확인/TripRequest JSON, Provider 상태·거점 매칭·제외 수 진단, 기술 설명, 중복 교통수단별 후보 섹션을 제거했다. 제목은 추천 교통편으로 변경했다. 카드의 내부 Provider 이름과 기술 주석도 표시하지 않는다.
서비스가 반환한 순서 그대로 최대 5개를 표시한다. 교통수단·등급·구간·날짜/시간·요금과 접근 이동·환승·거점 도착·승차 준비·버퍼·대기·총 소요시간은 유지했다. 선택한 교통편에는 구간·등급·출발/도착을 표시한다.
TripRequest, statuses, by_type, 전체 후보와 selected_transport는 내부 상태에 유지한다. 비즈니스 모델·Provider·서비스·설정은 Phase 3 ZIP과 바이트 비교하여 변경 없음을 확인했다. 추가 개인정보 로그는 없다.

## 검증
실행: .\.venv\Scripts\python -m pytest -q -p no:cacheprovider
결과: 152 passed, 2 skipped (exit code 0).
기존 선택/재조회/접근시간 표시 테스트 및 주변 장소 검색·재실행 캐시·재선택 시 무효화 테스트 통과.
추가 테스트는 제거 문구/JSON 비노출, 추천 제목, 최대 5개와 순서, 숨겨진 내부 결과 보존, 마지막 표시 후보 선택, 주변 장소 검색 버튼, 빈 결과 안내를 검증한다.
실제 외부 API 테스트 2개는 기본 실행에서 제외되어 이번 패치에서는 재호출하지 않았다. UI는 Streamlit AppTest로 검증했다.
테스트 종료 후 Windows 임시 디렉터리 정리 중 기존 PermissionError가 발생한다. 테스트 실패는 아니지만 환경 문제로 남아 있다.

## 남은 문제와 다음 우선순위
LOCAL_ORIGIN: 구로디지털단지역 → 구미 문제는 미해결이다. HubResolver.train은 TAGO 역 목록에 없는 ‘역’ 입력을 거점 후보로 확장하지 않고 빈 결과로 반환한다. 이번 UI 패치에서는 변경하지 않았다. 다음 패치에서 일반 출발지 좌표와 장거리 승차 거점을 분리하여 거점 탐색 후 접근시간/탑승 가능성을 검증하는 작업을 우선한다.


# LOCAL_ORIGIN 기능 PATCH

## 변경 파일
- models/access.py: AccessPoint에 기본값 있는 address/category 필드 추가.
- models/transport.py: TransportResult에 origin_status/user_message 기본값 필드 추가. TransportCandidate 변경 없음.
- services/access_service.py: 기존 장소 Resolve에서 주소 일치와 지하철 노선 접미사를 지원하고 주소/카테고리 보존.
- services/location_service.py: 출발지에만 적용하는 제한된 실제 TAGO 거점 선택 경로 추가. 도착지 매칭 유지.
- services/transport_service.py: 사용자 출발지 확인, 요청 단위 좌표 캐시, 거리 기반 거점 축소와 기존 노선/접근 검증 연결.
- config.py, .streamlit/secrets.toml.example: 검색 한도 설정.
- app.py: 출발지 확인 실패/조회 장애에 대한 일반 사용자 안내만 추가. UI PATCH 유지.
- tests/test_local_origin.py: 14개 Mock 기반 검증 추가. 기존 테스트 수정/삭제 없음.
- README.md: 본 보고 기록.

## 동작 방식
USER_ORIGIN은 Kakao 장소 검색으로 유일하게 확인된 AccessPoint다. 좌표·장소 ID·주소·카테고리를 보존한다. 이름/괄호를 뺀 이름/주소 일치, 역 카테고리와 노선 접미사를 확인하며 복수 일치 결과를 임의 선택하지 않는다.
구로디지털단지역은 지하철역 POI로 확인한 뒤 해당 POI 주소의 서울 지역 정보를 사용한다. TAGO 이름 불일치만으로 invalid 처리하지 않는다.
기존 TAGO 직접 매칭을 우선하며, 그 외 출발지에는 LOCAL_ORIGIN 거점 탐색을 적용한다. 실제 후보의 접근 출발/도착 POI ID가 같으면 DIRECT_HUB로 표시하며 기존 동일 장소 접근시간 0분과 승차 버퍼를 적용한다. 이 상태는 내부용이다. 후보가 없는 경우 DIRECT_HUB 확정 상태는 제공하지 않는다.
장소를 유일하게 확인하지 못하면 INVALID_ORIGIN과 구체적 입력 안내를 반환한다. Kakao 타임아웃 등 Provider 장애는 ORIGIN_UNAVAILABLE로 구분하며 재조회 안내를 반환한다.

## 실제 거점과 호출 제한
철도는 기존 TAGO 지역/역 목록, 버스는 확인된 도시의 TAGO 터미널 목록을 재사용한다. 역/터미널 ID나 후보 이름을 생성하거나 하드코딩하지 않는다.
목록에서 좌표를 확인한 거점들을 직선거리순으로 정렬하여 가까운 N개만 노선 조회에 사용한다. 직선거리로 이동시간을 만들지 않는다.
LOCAL_ORIGIN_HUB_SCAN_LIMIT 기본 12, 허용 1~30: 철도/버스 각각 신규 거점 좌표 조회 상한. 버스 두 종류는 예산과 좌표 캐시를 공유한다.
LOCAL_ORIGIN_HUB_LIMIT 기본 3, 허용 1~3: 교통수단별 거리순 출발 거점 상한. 기존 도착 거점 최대 3개와 결합해 수단별 노선 조회 최대 9조합이다. 시외버스 당일 제한도 유지한다.
좌표/실패 결과와 접근 경로를 동일 요청에서 캐싱한다. 실제 노선이 있고 기존 시간 필터를 통과한 후보만 정밀 접근시간을 조회한다. 신규 거점 좌표 조회는 전체 최대 24회(기본값)이며, 직접 매칭 거점의 기존 접근용 좌표 조회와 출발/도착 검색은 별도다. 최종 접근 경로는 후보 거점별 재사용한다.

## 탑승 검증 및 Ranking
기존 KakaoTransitProvider.fastest_route, AccessService.filter_candidates와 BoardingAssessment를 재사용한다. 접근시간+버퍼가 장거리 출발보다 늦으면 제외하며 경로 실패는 ACCESS_TIME_UNKNOWN으로 제외한다. 노선 없는 거점에는 접근 경로 API를 호출하지 않는다. 다른 거점/교통수단의 성공 결과는 유지한다.
기존 rank_candidates 함수는 수정하지 않았다. 총 이동시간은 실제 사용자 출발 가능 시각부터 장거리 도착까지로 접근·버퍼·추가 대기·장거리 이동을 포함한다. 같은 요청의 출발 가능 시각은 동일하므로 기존 도착 시각 우선순위는 총 이동시간 순서와 일치한다.

## 검증 결과
전체: 166 passed, 2 skipped, 종료 코드 0.
명령: .\.venv\Scripts\python -m pytest -q -p no:cacheprovider
서울역 → 구미: Mock 회귀에서 DIRECT_HUB, 접근 0분, 버퍼 15분 유지.
구로디지털단지역 → 구미 / 강남역 → 부산 / 일반 건물·주소 → 부산: Mock에서 LOCAL_ORIGIN으로 거점을 탐색하고 탑승 가능 후보 반환.
접근 30분+버퍼 15분 조건에서 09:30 편 제외, 10:00 편 유지. 노선 없는 거점은 접근 조회하지 않음.
서로 다른 대기/장거리 시간을 가진 후보의 총 210분/240분 순서 검증.
검색 한도, 모호한 장소, 지하철 노선 접미사, 존재하지 않는 장소, 접근 실패 및 버스 장애 격리, Kakao 장애와 잘못된 입력 구분 검증.
LOCAL_ORIGIN 서비스 결과를 Streamlit에서 선택한 뒤 기존 장소 검색으로 구미 도착 후보가 전달되고 장소가 표시되는 테스트 통과. PlaceService/Provider/기존 UI 테스트도 통과.

## 남은 제한사항
실제 API 통합 테스트 2개는 기존 opt-in 방식으로 제외되었고 이번 실행은 Mock/AppTest 검증이다. 실제 날짜의 배차 및 구로디지털단지역의 실시간 반환 결과는 확인하지 않았다.
검색 상한 내 TAGO 목록만 좌표화하므로 도시 전체의 가장 가까운 거점 또는 전국 최적 경로를 보장하지 않는다. 인접 도시까지 탐색하지 않으며 동명이인 장소는 구체적인 입력이 필요하다.
기존 Kakao 키워드 검색 결과로 확인되는 주소만 지원한다. 키워드 검색에 없는 순수 주소의 별도 주소 지오코딩 API는 추가하지 않았다.
접근시간의 정확도/가용성은 기존 경로 Provider에 의존하며 시간 추정 fallback은 없다. 지정 날짜 배차나 실시간 지연 보장은 기존과 동일하게 제공하지 않는다.
기존 Windows 임시 폴더 정리 PermissionError는 테스트 종료 후 계속 발생한다. 테스트 결과/종료 코드는 정상이며 관련 예외를 숨기거나 권한 로직을 변경하지 않았다.


# Phase 3 Place Discovery PATCH

## 변경 파일
app.py, models/place.py, services/place_service.py, tests/test_place_discovery.py(신규), README.md.
기존 테스트 파일, LOCAL_ORIGIN, TransportService/Ranking, AccessLeg, TAGO/Kakao/Groq Provider는 변경하지 않았다.

## 검색 범위와 역할
첫 검색은 search_places_for_trip → PlaceService.search_main으로 TripRequest.destination 지역 키워드를 검색한다. 좌표·radius 파라미터를 넣지 않는다. 주소의 행정구역 토큰을 정규화하여 여행지역 일치를 확인한다(경상북도/경북, 부산광역시/부산 등). 다른 지역 또는 확인할 주소가 없는 후보는 제외한다.
도착 거점은 여전히 result.anchor에 보존한다. 좌표 확인 실패 시 메인 검색을 계속하며 거리는 None으로 관리하여 0m로 표시하지 않는다. 이번 패치에서 첫 장소까지 이동시간이나 일정은 생성하지 않는다.
MAIN_DESTINATION과 NEARBY_PLACE 역할 필드를 추가했다. 메인 목록과 nearby_result 상태/목록은 분리했다. 기존 PlaceService.search는 반경 검색으로 유지하며 wrapper의 nearby=True로 호출한다.
사용자는 메인 후보 또는 도착 거점을 선택해 주변 장소를 별도 검색할 수 있다. 이때만 activity_radius(100~500m 또는 설정된 확장 반경)를 사용한다. 메인 장소 검색에는 적용하지 않는다.

## 쿼리·중복·점수
PREFERENCE_SEARCH_MAPPING에서 성향별 검색어를 관리한다. 관광은 ‘관광’, ‘관광명소’, ‘문화유적’, ‘테마거리’ 네 쿼리를 목적지와 조합한다. 다른 성향은 기존 검색어를 재사용하며 같은 매핑으로 확장 가능하다.
기존 페이지 상한 기본 2, 페이지당 15개, 최종 15개 제한을 재사용한다. 관광 단일 성향은 최대 8회 키워드 호출이며 15개 미만이면 해당 쿼리 페이지 탐색을 종료한다. 모든 10개 성향 선택 시 최대 26회이며 별도 도착 좌표 확인이 있다.
검증된 place_id로 중복 제거하고 matched_queries를 집합처럼 합쳐 같은 페이지/쿼리 중복을 과대평가하지 않는다. ID 없는 결과는 기존 필수값 검증으로 제외한다.
메인 점수는 지역 검증 통과 기본 20 + 검색 성향 수×10 + 카테고리 일치 성향 수(최대2)×20 + 고유 쿼리 수(최대4)×5 + 데이터 완전성(카테고리/주소/전화/링크)×2, 최대100이다. 점수 우선, 동점일 때 도착 기준 직선거리 등 기존 정렬을 재사용한다. 평점/방문객/인기도/실제 만족도는 생성하지 않는다. 기존 nearby 서비스 점수 계약은 유지했다.

## AI 및 UI
Groq는 검증된 상위 10개 후보 ID만 처리하며 외부 ID·잘못된 근거는 기존 검증에서 거부한다. 단순 검색 포함 근거는 표시하지 않고 카테고리 등 후보 데이터로 설명 가능한 근거만 표시한다. ‘AI 추천 이유’ 강조를 일반 ‘추천 근거’로 변경했다. 거점 좌표를 모르면 이번 구현에서는 AI 이유 호출을 생략한다.
‘구미 주요 추천 장소’와 ‘선택 장소·도착지 주변 추천’을 구분한다. 미선택 반려동물/아이/알레르기 조건은 숨기고 실제 입력 조건만 미확인으로 표시한다.
시간은 화면에서만 정수 분으로 반올림하고 소수가 있으면 ‘약’을 붙인다. 내부 수치와 탑승 계산은 그대로다. 실제 steps가 있을 때만 접힌 ‘접근 경로 구간’을 표시한다.

## 검증
전체 180 passed, 2 skipped, 종료 코드 0. 기존 166개 테스트를 수정하지 않고 신규 14개 검증을 추가했다.
명령: .\.venv\Scripts\python -m pytest -q -p no:cacheprovider
구미 Mock: 도착 거점 2km 밖의 구미 후보 유지, 활동반경 100m/500m 이상 모두 메인 조회에 영향 없음. 경주/성남 구미동 등 다른 지역 제외. 반복 쿼리 ID 중복 제거, 카테고리·검색 근거에 따른 점수 차이 확인.
메인/주변 역할·반경 분리, 거점 조회 실패 및 일부 API 실패 fallback, 조건 UI 숨김/표시, 주변 검색에 실제 선택 POI 전달, 빈 경로 UI 미표시, 시간 반올림 검증.
기존 LOCAL_ORIGIN 선택→장소 표시, transport ranking/access 및 전체 회귀 테스트 통과. 실제 외부 API 통합 테스트 2개는 opt-in으로 제외했으며 이번 구미 결과는 Mock/AppTest 검증이다.

## 제한사항
지역 전체를 검색 대상으로 하지만 Kakao 키워드 결과와 페이지 상한 내 후보이며 모든 관광지를 수집하거나 품질/인기도를 검증한 결과는 아니다.
목적지는 행정구역 이름 기준이다. ‘금오산’ 같은 시설명이나 비행정 별칭은 주소 토큰 검증을 통과하지 못할 수 있으므로 시·군·구 이름을 입력해야 한다. 복잡한 범용 지역 해석은 추가하지 않았다.
지역 점수는 검색 관련성 규칙이며 여행 만족도를 보장하지 않는다. 주변 검색은 기존 방식으로 선택 성향을 적용한다. 전체 일정/방문 순서/지도/이동시간 생성은 이번 범위에 없다.
기존 Windows 임시 폴더 정리 PermissionError는 테스트 종료 후 남아 있다. 테스트 통과/종료코드는 정상이며 예외를 숨기는 변경은 하지 않았다.


# Phase 3 음식점 추천 품질 PATCH

## 변경 파일
- services/place_service.py: 음식점 쿼리 다양화, 카페 제외, 식사 카테고리/쿼리 근거 점수, MAIN/NEARBY 정렬 분리, 구체적 사실 기반 추천 이유.
- app.py: 내부 점수 숨김, 기본 6개와 더 보기, 반경 설명, 의미 있는 접근 구간만 접힌 상세 표시.
- tests/test_food_quality.py: 신규 7개 검증.
- tests/test_places.py: 변경 요구와 충돌하는 고정 200점/과거 추천 문구 검증을 정확한 새 점수·근거 검증으로 갱신. 다중 쿼리 수에 맞춰 부분 실패 Mock 응답 확장.
- tests/test_places_live.py: 기존 반경 검색 통합 테스트를 명시적 nearby=True로 연결. 반경 assertion 유지.
- README.md: 본 보고 추가.
기존 모델, 교통 서비스/Ranking, LOCAL_ORIGIN, AccessLeg와 모든 Provider는 변경하지 않았다.

## Query와 필터
음식점은 MAIN/NEARBY 모두 음식점·맛집·한식 세 쿼리를 사용한다. PREFERENCE_SEARCH_MAPPING에서 관리한다. MAIN에는 destination을 접두어로 붙이고 좌표·radius를 전달하지 않는다. 결과 주소가 목적지 행정구역에 속하는지 기존 검증을 유지한다.
NEARBY만 선택 기준점 좌표와 activity_radius를 전달한다. 기존 기본 2페이지, 페이지당 15개 및 최종 15개 제한 유지. 음식점 단일 성향은 최대 6회 키워드 조회다.
place_id로 중복을 제거하며 matched_queries에는 서로 다른 쿼리를 한 번씩 보존한다. 같은 응답 안의 중복이나 같은 쿼리 페이지 중복은 가점이 늘어나지 않는다.
맛집 성향 처리에서는 카페/커피전문점 카테고리를 제외한다. 휴식 등 다른 선택 성향으로 검색된 카페는 해당 성향 후보로 남을 수 있으며 맛집 매칭 근거는 추가하지 않는다. 성향 목록 변경 없음.

## Score와 정렬
선호 점수는 고정값을 반환하지 않고 다음 실제 신호를 합산한다(최대100):
기본15 + 매칭 성향 수(최대2)×10 + 카테고리 일치 성향 수(최대2)×15 + 식사 세부 카테고리 일치8 + 카테고리 단계(최대3)×2 + 고유 쿼리 수(최대4)×4 + 쿼리 단어/카테고리 일치 수(최대3)×3 + 카테고리/주소/전화/링크 완전성×2 + MAIN 지역 검증5.
식사 카테고리는 한식·중식·일식·양식·분식·육류·해물·국밥·면류·뷔페를 사용한다. 한식을 다른 식사 유형보다 맛있다고 추정하지 않는다. 쿼리 일치는 검색 관련성일 뿐 평판/인기도가 아니다.
MAIN은 선호 점수→데이터 완전성→조건 충돌→ID 순서다. 도착 거점 거리의 정렬 영향을 제거했다.
NEARBY는 선호 점수 + 20/(1+직선거리m/200)의 별도 근접 기여도로 정렬한다. 선호 점수 자체에 거리를 저장하지 않으며 거리만으로 선호100점을 만들지 않는다. 동점에는 거리→조건 충돌→ID를 사용한다.
실제 근거가 동일한 후보는 같은 점수가 될 수 있다. 점수 차이를 강제하기 위해 임의 데이터를 만들지 않는다.

## UI와 AI
내부 선호 점수를 기본 화면에서 숨겼다. 카테고리·주소·전화·직선거리·관련 검색 근거를 표시한다. 구체적 카테고리와 거리(예: 직선거리 약111m)를 사용하며 도보시간·평점·실제 맛집 검증을 주장하지 않는다.
MAIN/NEARBY 각각 첫6개를 표시하고 나머지는 접힌 ‘더 보기’에 담는다. 모든 서비스 결과와 순서를 내부 상태에 유지한다.
확장 반경은 설정값을 읽어 ‘주변 검색 범위: 최대 2km’처럼 설명한다.
유효 mode와 양수 시간/거리 또는 안내가 있는 segment만 ‘접근 경로 자세히 보기’에 표시한다. segment가 없거나 빈 placeholder뿐이면 제목과 expander를 만들지 않는다. 접근 계산 데이터는 변경하지 않는다.
Groq의 후보 ID/근거 검증과 실패 fallback을 유지했다. 검증된 근접 근거도 이제 구체적 후보 카테고리·직선거리로 설명한다. 기본 UI의 사실 기반 근거는 AI 호출 없이도 표시된다.

## 검증
전체 187 passed, 2 skipped, 종료 코드0.
명령: .\.venv\Scripts\python -m pytest -q -p no:cacheprovider
기존 180개 검증 유지 및 요구 변경에 해당하는 기대값 갱신. 신규7개는 MAIN/NEARBY 음식점 점수 차이, 카페 제외, 휴식 선택 시 분리, 쿼리별 중복 근거, MAIN 거리 무관/NEARBY 거리 영향, UI 내부점수 숨김과 더 보기, 빈/실제 접근 구간을 검증한다.
LOCAL_ORIGIN 교통 선택→장소 검색 및 기존 MAIN/NEARBY 통합 테스트 통과.

## 실제 구미 API 검증
현재 로컬 Kakao 설정으로 구미 음식점 MAIN과 1위 장소 주변 NEARBY를 실행했다. 인증키·응답 개인정보는 보고/로그에 출력하지 않았다. 쿼리당1페이지로 제한했고 Groq와 실제 교통 시간표는 호출하지 않았다. 도착 거점은 구미 버스터미널을 지정한 교통 fixture로 좌표 확인했다.
MAIN 15개, 선호 점수 74/76/78/81/83/85, 카페0개, 주소 지역 검증 전부 통과, 터미널2km 밖 후보11개.
NEARBY 15개, 선호 점수 67/69/71/73/76/80, 카페0개, 모두 선택 장소2km 이내.
양쪽 상태는 페이지 상한 때문에 일부 결과다. 통계 원본은 outputs/FOOD_QUALITY_LIVE.json에 저장했다. 이는 실제 식당 평판을 검증한 결과가 아니다.

## 남은 제한사항
Kakao 검색 결과/페이지 상한의 편향은 존재한다. MAIN에 터미널 좌표를 전달하거나 거리를 정렬에 사용하지 않지만 도시의 모든 음식점을 망라하지 않는다. 다중 GIS anchor는 필요하지 않아 추가하지 않았다.
세부 카테고리나 연락처가 없는 후보의 근거는 제한적이다. 같은 근거는 같은 점수를 유지한다. 식사 목적 외 주점/디저트의 더 세밀한 분류는 별도 요구 시 보완 가능하다.
실제 테스트는 실행 시점 결과이며 추후 검색 결과는 달라질 수 있다. 오늘의 교통편/Groq 실호출 검증은 이번 작업에 포함되지 않았다.
기존 Windows 임시 폴더 정리 PermissionError는 전체 테스트 종료 후 여전히 발생한다. 테스트/종료코드는 정상이며 숨기기 위한 예외 처리는 추가하지 않았다.


# Transport Regression PATCH

## 원인과 수정
실제 TAGO 서울 역 목록은 12개였다. 기존 철도/버스 거점은 하나의 거리 순위에서 경쟁하지 않았고 철도 좌표 조회 예산도 독립이었다.
직접 원인은 AccessService.resolve_point가 ‘서울역’ 정확 일치 POI와 ‘서울역 1호선/4호선/공항철도’ 등 접미사가 있는 POI를 모두 동등한 matches에 넣어 모호한 결과로 거부한 것이다. 영등포역·용산역도 동일했다. LOCAL_ORIGIN 추가 당시 지하철 접미사 허용이 장거리 역의 좌표 해석까지 영향을 주었다. 좌표 미확인 거점은 사전 필터에서 사라져 노선 조회에 참여하지 못했다.
정확한 이름/기존 canonical 일치 결과를 접미사 결과보다 우선하도록 수정했다. 정확 일치가 여러 개이면 여전히 모호성 오류를 반환한다. 지하철-only 출발지와 기존 회사/건물/주소 Resolve를 유지한다. 역 이름 하드코딩 없음.
부가적으로 고속/시외버스가 공유하던 bool 기반 좌표 조회 예산을 TransportType별로 분리했다. 철도·고속·시외 각각 기본 12회 신규 좌표 조회, 최대3개 출발 거점을 보존한다. 좌표 캐시 자체는 공유하므로 동일 POI 중복 호출은 방지한다.
거점 목록 수·좌표 확인/미확인 수·예산 제외 수·선택 수를 수단별 내부 로그에 남기고 HubMatch.detail에 제외 이유를 보존한다. 사용자 UI는 변경하지 않았다. 사용자 주소나 API 인증키는 로그에 추가하지 않았다.

## 수정 파일
services/access_service.py, services/transport_service.py, tests/test_transport_regression.py(신규), README.md.
UI, PlaceService, MAIN/NEARBY 점수/검색, 모든 Provider, 모델, 기존 테스트 파일은 이전 ZIP과 바이트 비교하여 변경 없음을 확인했다. rank_candidates 함수와 AccessLeg/boarding 계산도 변경하지 않았다.

## 실제 2026-09-14 조회 (출발 가능09:00, 종료20:00)
구로디지털단지역 → 구미:
TAGO 지역 역 목록 → Kakao 좌표 확인 → 철도 목록 내 거리순 최대3개 → 영등포·용산·서빙고.
3개 거점에서 구미 노선 조회. 기존 시간 조건을 통과한 열차 후보10개, 접근 검증에서 추가 탈락 없음. 수단별 반환 상한5개 후 통합 추천에는 열차2개가 들어갔다. ‘10개’는 원시 API 행 전체가 아니라 시간/중복 등 기존 필터를 통과한 후보 수다.
서울역도 좌표 해석이 복구됐지만 LOCAL_ORIGIN의 가까운3개 제한에서는 위 거점에 밀려 제외된다. 따라서 서울에서 승차하는 대신 더 가까운 영등포에서 같은 도착시각의 열차가 추천된다. 용산·서빙고에서 유효 노선이 없으면 후보가 생기지 않으며 임의 일정을 만들지 않는다.

최종 상위5개:
1. 고속버스 프리미엄 서울경부10:25 → 구미13:25.
2. ITX-새마을 영등포10:33 → 구미13:27.
3. ITX-마음 영등포11:01 → 구미13:44.
4. 시외버스 우등 동서울11:10 → 구미14:00.
5. 고속버스 우등 서울경부11:15 → 구미14:15.
영등포 접근12분13초 + 버퍼15분 → 09:27:13 승차 준비 완료. 두 열차 모두 탑승 가능하다.
고속버스 접근33분40초, 시외버스 접근42분39초로 기존 API/검증을 적용했다. 수단에 따른 신규 순위 가중치 없음.

서울역 → 구미 회귀:
직접 서울 거점1개 조회, 시간 조건을 통과한 열차10개. 서울10:23→구미13:27 ITX-새마을과 서울10:48→구미13:44 ITX-마음이 최종 추천에 포함됐다. 접근0분, 버퍼15분 유지.
전체 수단 상태와 최종 후보는 outputs/TRANSPORT_REGRESSION_LIVE.json에 저장했다. 실제 날짜의 조회 결과이며 예매 가능 좌석을 확인한 결과는 아니다.

## 테스트
194 passed, 2 skipped, 종료코드0.
실행: .\.venv\Scripts\python -m pytest -q -p no:cacheprovider
기존187개 테스트 수정/삭제 없음. 신규7개: 서울/영등포/용산 정확POI와 지하철 혼합 및 진짜 모호성, LOCAL_ORIGIN 열차 포함/늦은 접근 탈락, 세 수단 조회예산 독립, 13:25버스→13:27열차→14:00버스 순서, 거점 실패 진단.
기존 서울역 DIRECT_HUB 및 LOCAL_ORIGIN→선택→PlaceService UI 연동, MAIN/NEARBY 검색, 음식점 품질 회귀 전부 통과했다. Phase3 API는 이번에 재호출하지 않았다. 기존 opt-in 테스트2개는 기본 전체 실행에서 제외됐으며 별도 실API transport 조회로 이번 문제를 검증했다.

## 제한사항
좌표를 유일하게 확인하지 못하는 일부 역은 계속 제외한다(실제 서울 목록에서5개). 탐색 상한/가까운3개 정책은 유지하므로 전체 철도망 최적 경로를 보장하지 않는다. 이번 패치는 교통수단 전체 누락의 원인을 수정했으며 무제한 탐색을 추가하지 않았다.
기존 Windows 임시 폴더 정리 PermissionError는 테스트 종료 후 남아 있다. 테스트 통과/종료코드는 정상이며 예외 숨김 변경 없음.


# Phase 4 일정 생성 완료 보고

## 생성/수정 파일
신규 제품 코드: models/schedule.py, services/schedule_service.py.
신규 테스트: tests/test_schedule.py, tests/test_schedule_ui.py, tests/test_schedule_live.py.
수정: app.py, config.py, .streamlit/secrets.toml.example, README.md.
실API 검증 결과: outputs/PHASE4_LIVE.json. 로컬 검증 실행 스크립트는 work/run_phase4_live.py(배포 ZIP 제외).
이전 Transport Regression ZIP과 바이트 비교했다. 기존 교통 서비스/Ranking, AccessLeg/LOCAL_ORIGIN, 모든 Provider, PlaceCandidate/PlaceService, 기존 테스트는 변경하지 않았다. 추가 라이브러리 없음.

## 모델과 시작/종료
TripSchedule은 timezone-aware trip_start_datetime/trip_end_datetime, arrival_point, typed items, 계산형 total_travel_minutes/total_activity_minutes, validation_status를 갖는다.
ScheduleItem은 TRAVEL/PLACE/MEAL, 실제 place_id/name, start/end, 계산형 duration, 이동 origin/destination POI 및 modes/route duration, 체류 추정 여부, preference/meal_slot, 근거와 확인 필요 정보를 갖는다. frozen Pydantic model로 관리하며 양수 시간/시간대 포함을 확인한다.
ARRIVAL은 TripSchedule 시작시각/arrival_point로 표현하고 타임라인의 도착 이벤트로 표시한다. 0분짜리 ScheduleItem을 생성하지 않으므로 모든 실제 항목은 start < end다.
시작은 selected_transport.arrival_time, 종료 제한은 TripRequest.end_date+end_time(KST). 실제 마지막 항목은 제한보다 일찍 끝날 수 있고 빈 시간은 자유시간으로 남긴다.

## 체류시간과 시간대 정책
ScheduleSettings 기본 추정값: 식사60분, 관광60분, 쇼핑60분, 체험90분, 휴식30분, 기타60분.
환경변수/Streamlit 설정의 SCHEDULE_MEAL_MINUTES, SCHEDULE_SIGHTSEEING_MINUTES, SCHEDULE_SHOPPING_MINUTES, SCHEDULE_EXPERIENCE_MINUTES, SCHEDULE_REST_MINUTES, SCHEDULE_DEFAULT_MINUTES로 조절 가능. 잘못된 값은 안전한 기본값으로 복귀한다.
체류시간은 estimated_duration=True이며 업체 제공 사실로 표현하지 않는다.
기본 활동09~20시, 야경18시 이후, 점심11~15시/저녁17~20시 내 체류가 완료되도록 한다. 시간대는 config.py의 ScheduleSettings에 모았다. 각 날짜별 식사 슬롯당 최대1곳이며 같은 장소 중복 방문도 금지한다. 맛집만 선택하면 관광지를 강제로 만들지 않는다.
현재 도착일 포함 최대8일, 전체 최대8방문으로 제한한다. 다일 여행도 날짜별 슬롯을 사용하지만 숙박 장소나 귀환 동선은 생성하지 않는다.

## 후보/경로 선택
기존 MAIN과 사용자가 이미 검색한 NEARBY 후보를 통합한다. ID 중복은 MAIN 역할을 보존한다. 선택한 장소, 성향별 상위 후보, NEARBY 대표 후보를 우선 보존하며 후보 상한15개를 적용한다.
MAIN/NEARBY의 원래 검색·점수 로직을 바꾸지 않는다. NEARBY는 현재 일정 위치의 activity_radius 안일 때 연결 후보가 된다. MAIN은 기본 직선거리20km 사전 필터를 적용하며 명시적으로 선택한 MAIN은 실제 경로 검증 기회를 준다.
기존 KakaoTransitProvider.fastest_route를 재사용해 도착거점→첫 장소, 장소→다음 장소를 계산한다. 같은 검증 POI ID이면 이동 생략이 가능하다. 직선거리로 이동시간을 만들지 않는다. 실패한 경로는 None으로 요청 내 캐시하고 다른 후보를 시도한다.
선호점수, 사용한 성향 수, 직선거리 사전 순서, 실제 이동/대기시간을 함께 사용한다. 순차적으로 최대3개 실행 가능한 대안을 비교하며 모든 순열을 계산하지 않는다. 기본 경로 호출 상한24개, 생성 검증 시도2회(MAX_SCHEDULE_ROUTE_CALLS / MAX_SCHEDULE_ATTEMPTS). 재시도 시 경로 성공/실패 캐시를 재사용한다.
이동은 방문 시작시간에 도착하도록 배치하므로 식사 슬롯까지 긴 공백이 있으면 이동 전 자유시간을 표시한다. 이동시간은 API 반환 정밀도를 그대로 보존한다.

## Groq와 fallback
Groq에는 선택 성향·도착/종료 시각·실제 후보 ID/카테고리/매칭 성향만 전달한다. 선호 순서의 ID 목록만 반환하도록 제한했다. 주소/영업시간/이동시간/일정 시각을 생성시키지 않는다.
Schema와 실제 후보 ID membership/중복을 검증한다. 유효한 순서는 제한된 보조 가점으로만 사용하며 최종 모든 이동·체류·시간대는 Python이 결정한다.
Groq timeout/429/5xx/invalid JSON/잘못된 ID는 기본 Python 일정으로 fallback한다. LLM 호출은 생성 요청당 최대1회다. Validation 실패 시 AI 보조를 제거하고 제한된 재시도 후에도 실패하면 schedule=None을 반환한다.
선택 장소는 강한 우선순위로 다루되 경로/시간대/종료 제약을 위반하면 제외하고 사용자 안내를 남긴다.

## Validator
실제 후보 ID·이름, 시작 경계와 종료 제한, 양수 체류, 항목 겹침, 방문 중복, POI 이동 연속성, 별도 경로 캐시의 실제 duration/modes와 TRAVEL 일치를 검증한다. 장소로 이동하지 않고 다른 POI에서 방문하는 순간이동을 거부한다.
식사 타입/슬롯별 유일성/설정 체류시간, 선택 성향 일치, 야경/활동 시간대도 재검증한다. open_status는 현재 UNKNOWN만 허용하며 필요한 영업/알레르기/반려동물/아이 확인 문구도 유지한다.
통과한 일정/항목에만 VALIDATED를 설정한다. 검증 실패 일정은 사용자 타임라인에 표시하지 않는다.
교통 미선택, 시간 부족, 후보 없음, 도착 좌표 미확인, 모든 경로 실패, 시간 조건 미충족, 검증 실패 상태를 구분하고 기술 Stack Trace 대신 안내를 반환한다.

## UI와 상태
Phase3 아래 ‘추천 여행 일정’/‘여행 일정 생성’ 버튼과 타임라인을 추가했다. 도착, 이동수단·예상 이동시간, 방문·기본 체류 추정, 자유시간, 확인 필요 정보와 종료시각을 표시한다.
점수·후보 index·Groq JSON·검증 내부 상태·raw_data는 표시하지 않는다.
trip_schedule과 schedule_result를 session_state에 저장한다. 교통 재선택, 여행조건 재제출, 주변 기준점 변경/검색, 메인 후보 재검색 시 무효화한다. 요청·교통·후보·선택 장소 내용의 fingerprint도 비교해 이전 결과를 재사용하지 않는다. 동일 조건 rerun은 API를 재호출하지 않는다.
Streamlit form의 입력 변경은 기존 방식대로 제출 시 적용된다. 주변 기준점은 명시적 선택 변경 또는 주변 검색 버튼을 누른 경우 일정 선호로 사용한다. 초기 자동 선택만으로 사용자 의도를 가정하지 않는다.

## 테스트 결과
전체 222 passed, 3 skipped, 종료코드0.
실행: .\.venv\Scripts\python -m pytest -q -p no:cacheprovider
기존194개 테스트 유지 + 신규28개 실행 검증. 신규 live 통합 테스트1개는 opt-in이며 기존 live2개와 함께 기본 실행에서 제외된다.
검증 범위: 도착 시작, 종료 제한, 첫 이동/장소간 이동, 중복 금지, 경로 실패 대안, Groq 실패/미등록ID fallback 및 정상 보조, 식사 제한/조건UNKNOWN, 짧은 시간0~1곳, 선택 장소 포함/시간 초과 제외, Validator 시간/이동/ID 변조 거부, 재시도/경로호출 상한, MAIN/NEARBY 역할, 다중 성향과 야경/다일 식사 슬롯, 모델·설정 검증.
AppTest에서는 실제 Mock LOCAL_ORIGIN 결과→교통 선택→PlaceCandidate→TripSchedule 생성과 교통/시간/장소/NEARBY 변경 무효화를 검증했다. 기존 Phase3 검색/선택 회귀도 전부 통과했다.

## 실제 구로디지털단지역 → 구미 검증
2026-09-14, 09:00 출발 가능, 20:00 종료, 맛집 성향. 실제 TAGO/Kakao와 Groq를 호출했다. 교통 추천1위 서울경부10:25→구미13:25를 선택하고 실제 구미 후보15개로 AI 보조 일정을 생성했다. 결과는 deterministic validation 통과.
최종 기록:
- 13:25 구미 도착.
- 13:25~13:38:34 도착 거점→싱글벙글복어 본점 이동(API 13분34초).
- 13:38:34~14:38:34 점심(기본 체류60분).
- 14:38:34~16:44:40 자유시간.
- 16:44:40~17:00 박가네왕갈비찜 이동(API 15분20초).
- 17:00~18:00 저녁(기본 체류60분).
- 18:00 일정 종료, 20:00 제한 이내.
이동 총28분54초, 체류120분. 실제 후보/경로 기반 일정이며 음식점 영업 중/좌석/알레르기 안전성 확인을 의미하지 않는다. 결과 원본은 outputs/PHASE4_LIVE.json. 검증 당시 검색 결과에 따라 장소가 달라질 수 있다.

## 제한사항과 다음 작업
경로 Provider는 지정 날짜/출발시각 입력을 지원하는 현재 인터페이스가 아니므로 API 예상 소요시간을 사용한다. 미래 배차·운행시간·지연 및 장소 영업시간은 보장하지 않는다. 확정 예약 일정이라고 표시하지 않는다.
무료/기본 체류시간은 정책 추정값이며 실제 장소 규모나 대기시간을 알지 못한다. 선택한 장소 중 사용할 수 있는 정보만 조합하며 일정을 억지로 채우지 않는다.
검색 후보15개, 방문8개, 경로24개, 직선거리 필터/활동시간/기간 상한 내 제한된 해이며 전역 최적 방문순서는 아니다. 여러 날짜도 숙박/휴무/귀가 교통을 생성하지 않는다.
NEARBY는 Phase3에서 검색한 후보만 재사용하고 일정 생성 중 추가 주변 장소 API 탐색을 하지 않는다. 다음 단계에서는 신뢰 가능한 영업시간·날짜별 이동경로·사용자 체류시간 조정과 일정 품질 평가 데이터를 우선 보완해야 한다.
지도, 실시간 재계획, DB, 로그인, 예약/결제는 추가하지 않았다.
기존 Windows 임시 폴더 정리 PermissionError는 테스트 종료 후 계속 발생한다. 테스트 결과/종료코드는 정상이며 예외 숨김이나 광범위한 권한 변경은 하지 않았다.


# Origin Resolver 안정화 PATCH

## 원인
양재역은 ‘양재역 3호선’·‘양재역 신분당선’이 함께 반환되면 기존 resolve_point의 단일 후보 조건을 만족하지 못했다. TAGO에 양재역이 없어서 실패한 것이 아니라 좌표 후보를 하나로 해석하지 못한 것이 직접 원인이다.
기존 일반 장소 해석은 원본/괄호 제거 이름 또는 주소의 완전 일치와 교통시설 별칭 위주였다. 지역명이 붙은 건물 입력이나 Keyword에 없는 주소를 처리할 fallback이 없었다. 건물의 교통 카테고리 여부 자체가 유일한 원인은 아니다. 사용자의 실제 건물명이 제공되지 않아 그 건물의 개별 실패 원인은 확정하지 않았다.

## 변경 파일
신규: services/origin_service.py, tests/test_origin_resolver.py.
수정: providers/kakao_provider.py, services/access_service.py, services/transport_service.py, models/access.py, app.py, tests/test_local_origin.py(Mock 주소 검색 기본 응답 추가), README.md.
기존 테스트 assertion은 변경하지 않았다. 기존 허브 탐색/수단별 예산/실제 노선/AccessLeg/승차 버퍼/Ranking/Phase3/Phase4 로직은 재작성하지 않았다.

## Resolve 순서
입력 공백 정리 → 일반 지시어(우리집/회사/현재위치 등) 거부 → 원본 Keyword Search 1회 → 반환 POI 검증/점수화 → 신뢰 가능한 단일 후보 또는 동일 역 묶음 → 필요할 때 Address Search 1회 → 좌표 확보 → 기존 DIRECT_HUB/LOCAL_ORIGIN 흐름.
새 AccessService.resolve_origin은 사용자 출발지 전용이다. 기존 resolve_point는 장거리 거점과 Phase4 도착 위치의 엄격한 식별 규칙으로 보존한다. TransportService는 검증된 origin을 기존 요청 단위 AccessCache에 넣어 재사용한다.

## Keyword 점수/정규화
정확/정규화 이름 일치120, 주소 일치110, 역 stem/카테고리 및 지역을 포함한 이름 일치100. 최소80과 상위 후보 간15점 차이를 기준으로 신뢰 후보를 결정한다. 조건을 충족하지 않는 결과는 API 순위1위라는 이유로 선택하지 않는다.
Unicode NFKC, 공백·특수문자·대소문자, 괄호 정보를 이름 비교에서 정리한다. 역 카테고리는 숫자호선/선명/GTX 접미사를 비교용으로 제거하되 ‘역’은 보존한다. 표시 이름은 원본 POI 이름을 유지한다.
양재역처럼 여러 노선 POI의 stem이 같고 교통 카테고리이며 주소의 시·구가 일치하고 모든 좌표 간 거리가300m 이내이면 같은 출발 역 묶음으로 판단한다. 점수와 ID의 안정적 순서로 실제 POI 하나를 사용한다. 임의 좌표 평균이나 전국 역 그래프를 만들지 않는다.
정확한 기차역 이름은 접미사 노선 결과보다 높은 점수로 우선한다. 멀리 떨어진 역·지역이 다른 역·동명이인 건물은 묶지 않는다.

## 건물과 모호성
일반 건물·호텔·아파트/오피스텔·회사·상점도 정확히 일치하는 이름과 유효 좌표가 있으면 카테고리 제한 없이 사용한다. ‘서울특별시 서초구 검증빌딩’은 장소명과 남은 지역 토큰을 주소에 대조한다. 시·도 표기 정규화를 재사용한다.
지역 없이 같은 건물명이 여러 개이고 신뢰 차이가 없으면 AMBIGUOUS_ORIGIN을 반환한다. 잘못된 한 지역을 무조건 확정하지 않는다. 구체적인 도로명/지번 주소 입력에 같은 주소의 업체가 여러 개 검색되는 경우는 주소 fallback으로 주소 자체 좌표를 사용한다.
LLM, GPS, 집 주소 자동 인식, DB는 사용하지 않는다.

## Address fallback
Kakao 공식 GET /v2/local/search/address.json을 기존 REST 키로 호출한다. analyze_type=exact, 한 페이지 최대30개. [공식 문서](https://developers.kakao.com/docs/ko/local/dev-guide#address-coord)를 확인했다.
ROAD_ADDR/REGION_ADDR와 유효 좌표·주소만 인정한다. REGION/ROAD 등 지역/도로 중심점은 거부한다. 주소가 여러 개이면 AMBIGUOUS_ORIGIN, 없으면 INVALID_ORIGIN. Keyword/API 장애는 일반 입력 오류로 숨기지 않고 ORIGIN_UNAVAILABLE로 구분한다. Keyword 장애라도 주소를 정확히 찾으면 사용할 수 있다.
주소에는 Kakao 장소ID가 없을 수 있으므로 검증된 좌표/주소로 만든 address: 접두어 내부 식별자를 사용한다. 이것은 Kakao POI ID라고 주장하지 않으며 외부 장소링크에도 사용하지 않는다.
AccessPoint에 original_query/source를 기본값 필드로 추가하고 기존 name/address/x/y/id/category를 재사용한다. source는 KAKAO_KEYWORD 또는 KAKAO_ADDRESS. 분류는 기존 TransportResult.origin_status에 보존한다.

## 캐시/UI
성공한 출발 좌표는 기존 AccessCache.points에서 접근 경로 계산에 재사용한다. Streamlit rerun은 기존 transport_result를 유지하므로 재조회하지 않는다. 변형 Keyword 요청/무한 재시도/전역 개인정보 캐시는 추가하지 않았다.
출발지 placeholder를 ‘역명, 건물명 또는 도로명 주소’로 변경했다. 모호/실패는 일반 사용자 안내만 제공한다. 기술 점수나 원본 응답은 표시하지 않는다. 인증키나 입력 주소를 로그에 추가하지 않았다.

## 실제 결과 (2026-09-14)
양재역: 실제 Kakao의 노선별 결과를 해석하여 ‘양재역 신분당선’ 원본 POI로 좌표 확보. 양재역→구미 LOCAL_ORIGIN 성공, 최종 교통편5개 반환.
구로디지털단지역: ‘구로디지털단지역 2호선’으로 Resolve, 구미행 최종5개(고속버스·열차·열차·시외버스·고속버스) 유지.
공개 주소 ‘서울특별시 중구 세종대로110’: KAKAO_ADDRESS fallback으로 ‘서울 중구 세종대로110’ 좌표 확보. 개인 거주지는 조회하지 않았다.
교통 회귀 결과는 outputs/ORIGIN_RESOLVER_LIVE.json에 저장했다. 실제 후보 수/순서는 날짜와 API 결과에 따라 달라질 수 있다. 양재역 추천에 열차가 없는 것은 이번 출발 좌표 실패와 별개로 기존 허브/노선/Ranking 제한을 유지한 결과다.

## 전체 테스트
243 passed, 3 skipped, 종료코드0. 기존222개 유지 및 신규21개 검증 추가.
명령: .\.venv\Scripts\python -m pytest -q -p no:cacheprovider
양재역 노선 묶음/순서 불변, 괄호/공백 정규화, 동명이인/먼 역 거부, 건물·호텔·회사·상점 LOCAL_ORIGIN 및 AccessLeg, 지역 포함 건물명 선택, 주소 fallback/ID 없음/지역 중심점 거부, Keyword 장애 fallback, 주소 Provider contract/오류, 일반 지시어 거부 등을 검증했다.
기존 구로디지털단지역 회귀와 LOCAL_ORIGIN→선택→Phase3→Phase4 Schedule 테스트도 통과했다. 기본 실행의 live3개는 opt-in 제외이며 이번 실제 테스트는 별도 실행했다.

## 제한사항
제공되지 않은 실제 거주 건물명은 검증하지 않았다. 오타/별칭/부분 이름이 많거나 행정구역이 불충분하면 구체적 건물명·주소 입력이 필요하다. 낮은 신뢰 결과를 성공시키기 위한 임의 선택은 하지 않는다.
동일 역 판정의300m/시·구 기준은 보수적이다. 행정 경계를 가로지르는 환승역이나 더 넓은 역은 여전히 모호성 안내가 나올 수 있다. 노선별 승강장 내부 이동시간을 별도로 보장하지 않는다.
Keyword 최대15개/Address 최대30개 응답 범위에서 확인하며 검색 결과 전체를 무제한 조회하지 않는다. 주소의 내부 식별자는 실제 장소 POI와 자동으로 동일시하지 않는다.
기존 Windows 임시 폴더 정리 PermissionError는 테스트 종료 후 남아 있다. 테스트/종료코드는 정상이며 예외를 숨기지 않았다.


## 건물명 주소 입력 PATCH (2026-09-15)
Keyword 검색은 원본 포함 최대 3회 공백 변형을 시도한다. 미발견 건물명은 NEED_ADDRESS 상태에서만 주소 폼을 표시한다. 입력 주소를 Kakao Address Search로 검증한 후 기존 LOCAL_ORIGIN 교통 검색을 재사용한다. 상세 변경과 252 passed / 3 skipped 결과는 outputs/ORIGIN_ADDRESS_FLOW_REPORT.md 참조.


## Phase 4 Supporting Schedule + Compact UI PATCH
60분 이상 공백에는 실제 주변 후보와 양쪽 경로를 검증해 방문을 추가한다. MIN_SUPPORTING_ACTIVITY_GAP_MINUTES와 LATE_LUNCH_END_HOUR로 기준을 관리한다. 선택 교통편은 한 카드로 표시하고 대안은 접힌 목록으로 제공한다. 상세 검증: outputs/SUPPORTING_SCHEDULE_REPORT.md (260 passed, 3 skipped).


## Iterative Gap Filling PATCH
MAX_GAP_FILL_ITERATIONS 기본 4회, GAP_SAFETY_BUFFER_MINUTES 기본 5분. 매 삽입 후 검증하고 카테고리 다양성을 고려한다. 검색 기본값과 사용자 명시 선호를 구분하며 공백은 여유시간으로 표시한다. 상세: outputs/ITERATIVE_GAP_REPORT.md (264 passed, 3 skipped).


## Route Ranking PATCH
방문 순서는 실제 이동 분당 1점 감점을 적용한다. Supporting 후보는 현재/다음 장소 양쪽에서 검색하고 실제 추가 이동 및 우회를 비교한다. 시간 슬롯이 소진된 후보의 불필요한 경로 호출을 제외한다. 상세: outputs/ROUTE_RANKING_REPORT.md (268 passed, 3 skipped).
