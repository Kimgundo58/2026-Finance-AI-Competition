# API · DB 명세 v1

이 문서는 무엇이 지금 있는가를 적는다. 엔드포인트 목록과 스키마를 찾아보는 색인이다.
찾을 것이 정해져 있으면 목차에서 바로 내려가라.

> 🔴 **이 문서는 "지금 실제로 무엇이 있는가"다.** "왜 이렇게 주는가"(계약·근거·
> 금지사항)는 `docs/7_백엔드/API_계약_v1.0.md` 가 기준이고, **충돌하면 계약 문서가 이긴다.**
>
> 손으로 쓴 문서가 아니다. API 는 `app.openapi()`, DB 는 `information_schema` +
> `pg_constraint` **실조회**에서 생성했다. 스키마가 바뀌면 이 파일을 고치지 말고
> 생성기를 다시 돌려라.
>
> ```bash
> PYTHONIOENCODING=utf-8 python db/tools/gen_api_db_spec.py     # 저장소 루트에서
> ```
>
> 생성 시각 `2026-09-02T01:17:58+09:00` · 소스 `server.main:app` · `localhost:5432/suddoe` · 생성기 `db/tools/gen_api_db_spec.py`


## 0. 두 문서를 어떻게 나눠 읽는가

| | `docs/7_백엔드/API_계약_v1.0.md` | **이 문서** |
|---|---|---|
| 답하는 질문 | 왜 이렇게 주는가 | 지금 실제로 무엇이 있는가 |
| 담는 것 | 계약·근거·금지사항·미결 주체 | 스펙·스키마·타입·제약 |
| 갱신 | 사람이 판단해서 고친다 | **재생성한다** |
| 충돌 시 | **이긴다** | 진다 |

🔴 **여기 있는 것이 다 굳은 것은 아니다.** 무엇이 동결이고 무엇이 아직 값이 안
채워졌고 무엇이 미결인지는 **`docs/7_백엔드/계약_정정_0902.md` 가 기준**이다. 코드를 얹기
전에 그 문서를 먼저 봐라 — 이 문서에 컬럼이 있다는 것과 그게 계약이라는 것은 다르다.
(확정도를 여기에도 적으면 둘 중 하나가 먼저 낡는다. 그래서 안 적는다.)

계약 문서가 "만들지 않는 것"(§8)이라 못 박은 기능은 이 문서에 스키마가 있어도
만들지 않는다.


## 1. API — 실측 (`app.openapi()` + 라우트 객체)

### 1-1. 엔드포인트

🔴 `text/event-stream` 인 둘은 응답 본문이 SSE 라 스키마가 없다 — §2 를 봐라.

| 메서드 | 경로 | 요청 | 응답(2xx) |
|---|---|---|---|
| GET | `/admin/cost` | — | dict |
| GET | `/admin/gate` | — | dict |
| GET | `/admin/queue` | — | dict |
| POST | `/admin/warmup` | — | dict |
| GET | `/api/health` | — | dict |
| POST | `/api/judge` | `판정요청` | **SSE** — §2 |
| POST | `/api/l3/upload` | multipart (`파일`, `org_id`, `기관명`) | `L3업로드응답` (202) |
| GET | `/api/l3/{doc_id}` | — | `L3업로드응답` |
| POST | `/api/normalize` | `정규화요청` | **SSE** — §2 |
| GET | `/api/plans` | — | `계획목록응답` |
| POST | `/api/plans` | `계획생성` | `계획상세` (201) |
| GET | `/api/plans/{plan_id}` | — | `계획상세` |
| POST | `/api/plans/{plan_id}/tasks` | `할일생성` | `할일` (201) |
| PATCH | `/api/plans/{plan_id}/tasks/{task_id}` | `할일수정` | `할일` |
| POST | `/api/plans/{plan_id}/tasks:sync` | `할일동기화` | `할일동기화응답` |
| GET | `/api/profile` | — | dict |
| PUT | `/api/profile` | `프로필` | dict |
| GET | `/api/programs` | — | dict |
| GET | `/api/tasks` | — | `할일목록응답` |
| GET | `/api/vocab` | — | dict |

### 1-2. 쿼리 · 경로 파라미터

OpenAPI `parameters` 실측. `기본값` 이 있으면 안 보내도 된다.

**`GET /admin/cost`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `x-admin-token` | header | str \| null |  | — |  |

**`GET /admin/gate`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `x-admin-token` | header | str \| null |  | — |  |

**`GET /admin/queue`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `종류` | query | str \| null |  | — |  |
| `사유코드` | query | str \| null |  | — |  |
| `사업명` | query | str \| null |  | — |  |
| `상태` | query | str |  | `'대기'` |  |
| `limit` | query | int |  | `50` |  |
| `offset` | query | int |  | `0` |  |
| `x-admin-token` | header | str \| null |  | — |  |

**`POST /admin/warmup`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `x-admin-token` | header | str \| null |  | — |  |

**`POST /api/judge`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `목` | query | str \| null |  | — |  |

**`GET /api/l3/{doc_id}`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `doc_id` | path | str | ✅ | — |  |
| `org_id` | query | str \| null |  | — |  |

**`GET /api/plans`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `탭` | query | str |  | `'전체'` | 전체·확인필요·위험·특이사항없음·점검전 |
| `사업명` | query | str \| null |  | — |  |
| `확정비목` | query | str \| null |  | — |  |
| `q` | query | str \| null |  | — | 지출명 검색 |
| `금액_최소` | query | float \| null |  | — |  |
| `금액_최대` | query | float \| null |  | — |  |
| `정렬` | query | str |  | `'최근수정순'` | 최근수정순·금액많은순·금액적은순·지출일순 |
| `페이지` | query | int |  | `1` |  |
| `크기` | query | int |  | `20` |  |
| `org_id` | query | str \| null |  | — |  |

**`GET /api/plans/{plan_id}`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `plan_id` | path | int | ✅ | — |  |
| `org_id` | query | str \| null |  | — |  |

**`POST /api/plans/{plan_id}/tasks`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `plan_id` | path | int | ✅ | — |  |
| `org_id` | query | str \| null |  | — |  |

**`PATCH /api/plans/{plan_id}/tasks/{task_id}`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `plan_id` | path | int | ✅ | — |  |
| `task_id` | path | int | ✅ | — |  |
| `org_id` | query | str \| null |  | — |  |

**`POST /api/plans/{plan_id}/tasks:sync`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `plan_id` | path | int | ✅ | — |  |
| `org_id` | query | str \| null |  | — |  |

**`GET /api/profile`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `org_id` | query | str \| null |  | — |  |

**`PUT /api/profile`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `org_id` | query | str \| null |  | — |  |

**`GET /api/tasks`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `상태` | query | str \| null |  | — | 준비필요·집행예정·완료 |
| `구분` | query | str \| null |  | — | 결제전·결제후·집행 |
| `plan_id` | query | int \| null |  | — |  |
| `일정만` | query | bool |  | `False` | true 면 due_date 가 있는 행만 (캘린더) |
| `이후` | query | str \| null |  | — | YYYY-MM-DD — 이 날짜 이후만 |
| `org_id` | query | str \| null |  | — |  |

**`GET /api/vocab`**

| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:--:|---|---|
| `사업명` | query | str \| null |  | — |  |


### 1-3. 요청·응답 스키마

`server/models.py` 의 pydantic 모델에서 직접 뽑았다. 필수 = 기본값이 없는 필드.

**`F1`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `정부지원_현금` | float |  | `0` |
| `자기부담_현금` | float |  | `0` |
| `협약시작일` | str \| null |  | — |
| `협약종료일` | str \| null |  | — |

**`F3항`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `비목` | str | ✅ | — |
| `재원` | `정부지원` \| `자기부담` | ✅ | — |
| `거래처` | str \| null |  | — |
| `인력역할` | str \| null |  | — |
| `귀속월` | str \| null |  | — |
| `금액` | float |  | `0` |

**`F4항`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `역할` | str | ✅ | — |
| `고용형태` | str \| null |  | — |
| `타사업참여율` | float |  | `0` |
| `소속기관유형` | str \| null |  | — |
| `겸직` | bool |  | `False` |

**`F5`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `친족거래` | bool |  | `False` |
| `전직임직원업체` | bool |  | `False` |

**`L3업로드응답`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `doc_id` | str | ✅ | — |
| `파일명` | str | ✅ | — |
| `확장자` | str | ✅ | — |
| `상태` | `파싱대기` \| `파싱중` \| `완료` \| `실패` |  | `'파싱대기'` |
| `조_건수` | int \| null |  | — |
| `dangling` | dict[str, Any][] |  | `[]` |
| `메시지` | str \| null |  | — |

**`계획목록응답`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `통계` | `계획통계` |  | `계획통계(전체=0, 확인필요=0, 위험=0, 특이사항없음=0, 점검전=0, 금액합계=0)` |
| `건수` | int |  | `0` |
| `페이지` | int |  | `1` |
| `크기` | int |  | `20` |
| `항목` | `계획요약`[] |  | `[]` |

**`계획상세`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `plan_id` | int | ✅ | — |
| `제목` | str \| null |  | — |
| `확정비목` | str \| null |  | — |
| `금액` | float \| null |  | — |
| `판정` | `가능` \| `조건부` \| `불가` \| `판단불가` \| null |  | — |
| `집행예정일` | str \| null |  | — |
| `updated_at` | str \| null |  | — |
| `사업명` | str \| null |  | — |
| `상태` | `draft` \| `judged` |  | `'draft'` |
| `질문원문` | str \| null |  | — |
| `용도` | str \| null |  | — |
| `거래처` | str \| null |  | — |
| `추가설명` | str \| null |  | — |
| `정규화` | dict[str, Any] |  | `{}` |
| `latest_decision_id` | int \| null |  | — |
| `판정상세` | dict[str, Any] \| null |  | — |
| `할일` | `할일`[] |  | `[]` |
| `created_at` | str \| null |  | — |

**`계획생성`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `사업명` | str | ✅ | — |
| `제목` | str \| null |  | — |
| `품목` | str | ✅ | — |
| `금액` | float | ✅ | — |
| `용도` | str | ✅ | — |
| `집행예정일` | str \| null |  | — |
| `거래처` | str \| null |  | — |
| `추가설명` | str \| null |  | — |
| `확정비목` | str \| null |  | — |
| `정규화` | dict[str, Any] |  | `{}` |
| `질문원문` | str \| null |  | — |
| `org_id` | str \| null |  | — |

**`계획요약`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `plan_id` | int | ✅ | — |
| `제목` | str \| null |  | — |
| `확정비목` | str \| null |  | — |
| `금액` | float \| null |  | — |
| `판정` | `가능` \| `조건부` \| `불가` \| `판단불가` \| null |  | — |
| `집행예정일` | str \| null |  | — |
| `updated_at` | str \| null |  | — |
| `사업명` | str \| null |  | — |
| `상태` | `draft` \| `judged` |  | `'draft'` |

**`계획통계`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `전체` | int |  | `0` |
| `확인필요` | int |  | `0` |
| `위험` | int |  | `0` |
| `특이사항없음` | int |  | `0` |
| `점검전` | int |  | `0` |
| `금액합계` | float |  | `0` |

**`정규화요청`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `품목` | str \| null |  | — |
| `금액` | float \| null |  | — |
| `용도` | str \| null |  | — |
| `집행예정일` | str \| null |  | — |
| `거래처` | str \| null |  | — |
| `추가설명` | str \| null |  | — |
| `질문` | str \| null |  | — |
| `사업명` | str \| null |  | — |
| `f5` | `F5` |  | `F5(친족거래=False, 전직임직원업체=False)` |

**`판정요청`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `정규화` | dict[str, Any] |  | `{}` |
| `확정비목` | str \| null |  | — |
| `사업명` | str \| null |  | — |
| `org_id` | str \| null |  | — |
| `plan_id` | int \| null |  | — |
| `f5` | `F5` |  | `F5(친족거래=False, 전직임직원업체=False)` |

**`프로필`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `f1` | `F1` |  | `F1(정부지원_현금=0, 자기부담_현금=0, 협약시작일=None, 협약종료일=None)` |
| `f3` | `F3항`[] |  | `[]` |
| `f4` | `F4항`[] |  | `[]` |

**`할일`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `task_id` | int | ✅ | — |
| `plan_id` | int \| null |  | — |
| `출처` | `ai` \| `user` |  | `'ai'` |
| `코드` | str \| null |  | — |
| `구분` | `결제전` \| `결제후` \| `집행` |  | `'결제전'` |
| `항목` | str | ✅ | — |
| `설명` | str \| null |  | — |
| `due_date` | str \| null |  | — |
| `유형` | `기타` \| `계약` \| `비교견적` |  | `'기타'` |
| `날짜_사용자수정` | bool |  | `False` |
| `상태` | `준비필요` \| `집행예정` \| `완료` |  | `'준비필요'` |
| `계획제목` | str \| null |  | — |

**`할일동기화`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `decision_id` | int \| null |  | — |
| `해야할일` | dict[str, Any][] |  | `[]` |

**`할일동기화응답`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `생성` | int |  | `0` |
| `갱신` | int |  | `0` |
| `보존_user` | int |  | `0` |
| `보존_날짜수정` | int |  | `0` |
| `코드매칭` | int |  | `0` |
| `코드미상` | int |  | `0` |

**`할일목록응답`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `건수` | int |  | `0` |
| `항목` | `할일`[] |  | `[]` |

**`할일생성`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `항목` | str | ✅ | — |
| `설명` | str \| null |  | — |
| `구분` | `결제전` \| `결제후` \| `집행` |  | `'결제전'` |
| `due_date` | str \| null |  | — |
| `유형` | `기타` \| `계약` \| `비교견적` |  | `'기타'` |

**`할일수정`**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|:--:|---|
| `상태` | `준비필요` \| `집행예정` \| `완료` \| null |  | — |
| `due_date` | str \| null |  | — |
| `유형` | `기타` \| `계약` \| `비교견적` \| null |  | — |


## 2. SSE — OpenAPI 에 안 잡히는 부분

`/api/normalize` 와 `/api/judge` 는 `text/event-stream` 이라 OpenAPI 가 본문 스키마를
못 준다. 아래는 **계약 문서 §5·§6 의 인용**이고, `tests/test_contract.py` 가 순서와
키를 검증한다. 출처: `docs/7_백엔드/API_계약_v1.0.md`.

### `POST /api/judge` — 이벤트 8종, 이 순서

```text
event: 진행       {"단계":"검색|룰조회|조립", "설명":"..."}      ← 3회
event: 판정       {판정, 요약, 신뢰등급, 버전스탬프}
event: 해야할일    [ {항목, 설명}, ... ]
event: 인용       [ {조번호, 조제목, 원문, doc_id}, ... ]
event: 전제       [ {사실, 근거조항, 매핑[], 미충족시}, ... ]
event: 참조사슬    [ {from{doc_id,조번호}, 표기, 관계, to{...}, 보정}, ... ]
event: 문의초안    "..."        ← 🔴 판정이 「판단불가」일 때만. 참조사슬과 결과 사이
event: 결과       { 전체 JSON }                ← 이것 하나만 들어도 화면이 그려진다
event: 저장       {저장: bool, 사유?: str, ...}
event: 완료       {캐시: bool}
```

`저장: false` **는 실패가 아니다.** 아래 셋은 전부 정상 경로이고, 빨간 배너로
그리면 안 된다 — `plan_id 없음` · `캐시 적중 — 새 판정 기록 없음` ·
`decision_id 없음 — 판정이 기록되지 않았다`.

`결과` 이벤트에 `decision_id` 는 **실리지 않는다.** 캐시·응답에 박히면 다른 요청이
남의 판정 기록을 자기 계획에 가리키게 된다(TENANT_LEAK 류). 회귀 테스트로 잠가 뒀다.

`?목=가능|조건부|불가|판단불가` 로 4-way 화면을 전부 그려볼 수 있다.

### `POST /api/normalize`

```text
event: 진행   {"단계":"정규화","설명":"질문에서 사실을 뽑는 중"}
event: 필드   {품목|금액|금액_추정여부|용도|비목후보 중 하나}   ← 스트리밍 렌더용. 안 들어도 된다
event: 결과   { 전체 JSON }
event: 완료   {"캐시": bool}
```

실패하면 `event: 오류` → `event: 완료 {"실패":true}`. **500 을 던지지 않는다** —
모든 실패의 기본값은 판단불가다.

### 오류 봉투 — 전부 한 모양

```json
{ "오류": "지출계획 999 을(를) 찾을 수 없습니다", "상태": 404 }
{ "오류": "...", "상태": 422, "필드": null }        ← 422 만 `필드` 가 붙는다
```

pydantic 기본 `{"detail":[...]}` 는 서버가 걷어냈다. 렌더러는 한 벌이면 된다.


## 3. DB 스키마 (실조회)

### 소유와 경계

- **`tenant.*` 는 우리(프론트–백엔드) 소유다.** 읽고 쓴다.
- **`corpus.*` 는 Agent 세션 소유다 — 이 문서에서는 "읽기 참조".**
  우리가 실제로 쓰는 것은 `check_items` 하나뿐이다(할일 코드·구분·유형·오프셋일).
  나머지는 판정 파이프라인이 쓰는 표이고, **여기서 구조를 바꾸자고 하면 안 된다.**
- `eval.*` 은 평가 전용이라 이 문서 범위 밖이다.

표기 — `NN` = NOT NULL · `PK` = 기본키 · `FK→` = 외래키 · `CHECK` 는 표 아래 별도.

⚠️ **행수는 재생성 시점의 스냅샷이다 — 계약이 아니다.** 특히 `expense_plans` 의
현재 행은 테스트 픽스처 찌꺼기가 섞여 있어 실사용 데이터가 아니다. 컬럼·타입·제약만
믿어라.


### 3-1. `tenant.*` — 우리 소유 (읽기·쓰기)

#### `tenant.accounts`  · 5컬럼 · 0행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `account_id` | uuid | PK | `gen_random_uuid()` |
| `org_id` | uuid | NN · FK→orgs(org_id) | — |
| `email` | text | NN | — |
| `pw_hash` | text | NN | — |
| `created_at` | timestamptz | NN | `now()` |

- UNIQUE — `UNIQUE (email)`

#### `tenant.decisions`  · 27컬럼 · 73행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `decision_id` | bigint | PK | `nextval('decisions_decision_id_seq'::regclass)` |
| `created_at` | timestamptz | NN | `now()` |
| `org_id` | uuid | FK→orgs(org_id) | — |
| `사업명` | text |  | — |
| `기관id` | text |  | — |
| `질문원문` | text | NN | — |
| `정규화` | jsonb |  | — |
| `비목` | text |  | — |
| `금액` | numeric |  | — |
| `판정` | text |  | — |
| `신뢰등급` | text |  | — |
| `인용` | jsonb |  | — |
| `해야할일` | jsonb |  | — |
| `지연ms` | jsonb |  | — |
| `모델` | jsonb |  | — |
| `전제` | jsonb |  | — |
| `검색스냅샷` | jsonb |  | — |
| `코퍼스버전` | text |  | — |
| `plan_id` | bigint | FK→expense_plans(plan_id) | — |
| `요약` | text |  | — |
| `버전스탬프` | text |  | — |
| `참조사슬` | jsonb |  | — |
| `강등사유` | ARRAY |  | — |
| `미매핑전제` | jsonb |  | — |
| `강등코드` | ARRAY | NN | `'{}'::text[]` |
| `경로` | text |  | — |
| `실패단계` | text |  | — |

- CHECK `decisions_강등코드_check` — `(("강등코드" <@ ARRAY['INVALID_JUDGMENT'::text, 'CITE_NOT_IN_MAP'::text, 'CITE_DB_MISSING'::text, 'CITE_HANG_MISMATCH'::text, 'PREMISE_NO_BASIS'::text, 'PREMISE_BASIS_NOT_IN_MAP'::text, 'PREMISE_ENUM'::text, 'PREMISE_UNMAPPED'::text, 'NO_CITATION'::text, 'VLM_DOWNGRADE'::text, 'B_GRADE_DOWNGRADE'::text, 'UNVERIFIED_RULE'::text, 'TASK_CODE_INVALID'::text, 'L3_ONLY_DOWNGRADE'::text, 'TENANT_LEAK'::text, 'DANGLING_WARN'::text, 'DOMAIN_WARN'::text, 'PRECEDENCE_FLIP'::text, 'TASK_STATE_UNSOURCED'::text, 'TASK_STATE_MISMATCH'::text, 'TASK_NUMBER_UNSOURCED'::text, 'TASK_BASIS_NOT_IN_MAP'::text]))`
- CHECK `decisions_신뢰등급_check` — `((("신뢰등급" = ANY (ARRAY['A'::text, 'B'::text])) OR ("신뢰등급" IS NULL)))`
- CHECK `decisions_판정_check` — `((("판정" = ANY (ARRAY['가능'::text, '조건부'::text, '불가'::text, '판단불가'::text])) OR ("판정" IS NULL)))`

#### `tenant.expense_plans`  · 15컬럼 · 0행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `plan_id` | bigint | PK | `nextval('expense_plans_plan_id_seq'::regclass)` |
| `org_id` | uuid | FK→orgs(org_id) | — |
| `제목` | text |  | — |
| `질문원문` | text | NN | — |
| `정규화` | jsonb |  | — |
| `확정비목` | text |  | — |
| `금액` | numeric |  | — |
| `집행예정일` | date |  | — |
| `거래처` | text |  | — |
| `상태` | text | NN | `'draft'::text` |
| `latest_decision_id` | bigint | FK→decisions(decision_id) | — |
| `created_at` | timestamptz | NN | `now()` |
| `updated_at` | timestamptz | NN | `now()` |
| `사업명` | text |  | — |
| `추가설명` | text |  | — |

- CHECK `expense_plans_상태_check` — `(("상태" = ANY (ARRAY['draft'::text, 'judged'::text])))`

#### `tenant.f_exec`  · 9컬럼 · 0행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `exec_id` | bigint | PK | `nextval('f_exec_exec_id_seq'::regclass)` |
| `profile_id` | uuid | NN · FK→f_profile(profile_id) | — |
| `비목` | text | NN | — |
| `재원` | text | NN | — |
| `거래처` | text |  | — |
| `인력역할` | text |  | — |
| `귀속월` | date |  | — |
| `금액` | numeric | NN | — |
| `created_at` | timestamptz | NN | `now()` |

- CHECK `f_exec_재원_check` — `(("재원" = ANY (ARRAY['정부지원'::text, '자기부담'::text])))`

#### `tenant.f_personnel`  · 7컬럼 · 0행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `person_id` | bigint | PK | `nextval('f_personnel_person_id_seq'::regclass)` |
| `profile_id` | uuid | NN · FK→f_profile(profile_id) | — |
| `역할` | text | NN | — |
| `고용형태` | text |  | — |
| `타사업참여율` | numeric |  | — |
| `소속기관유형` | text |  | — |
| `겸직` | boolean |  | — |

- CHECK `f_personnel_역할_check` — `(("역할" = ANY (ARRAY['대표자'::text, '신규채용'::text, '기존직원'::text])))`

#### `tenant.f_profile`  · 9컬럼 · 0행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `profile_id` | uuid | PK | `gen_random_uuid()` |
| `org_id` | uuid | NN · FK→orgs(org_id) | — |
| `사업명` | text | NN | — |
| `협약시작일` | date |  | — |
| `협약종료일` | date |  | — |
| `정부지원_현금` | numeric |  | — |
| `자기부담_현금` | numeric |  | — |
| `과업범위요약` | text |  | — |
| `updated_at` | timestamptz | NN | `now()` |

- UNIQUE — `UNIQUE (org_id, "사업명")`

#### `tenant.incidents`  · 10컬럼 · 0행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `incident_id` | bigint | PK | `nextval('incidents_incident_id_seq'::regclass)` |
| `발생시각` | timestamptz | NN | `now()` |
| `종류` | text | NN | — |
| `decision_id` | bigint | FK→decisions(decision_id) | — |
| `org_id` | uuid | FK→orgs(org_id) | — |
| `기대_기관id` | text |  | — |
| `발견_기관id` | text |  | — |
| `질문원문` | text |  | — |
| `상세` | jsonb | NN | `'{}'::jsonb` |
| `해소` | boolean | NN | `false` |

- CHECK `incidents_종류_check` — `(("종류" = ANY (ARRAY['TENANT_LEAK'::text, 'INDEX_GUARD'::text, 'SCHEMA_VIOLATION'::text, 'ROUTING_BLOCK'::text, '기타'::text])))`

#### `tenant.l3_articles`  · 9컬럼 · 17행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `article_id` | bigint | PK | `nextval('l3_articles_article_id_seq'::regclass)` |
| `doc_id` | uuid | NN · FK→l3_documents(doc_id) | — |
| `org_id` | uuid | NN | — |
| `조번호` | text | NN | — |
| `조제목` | text |  | — |
| `조번호_int` | integer |  | — |
| `장` | text |  | — |
| `본문` | text | NN | — |
| `페이지` | integer |  | — |

- UNIQUE — `UNIQUE (doc_id, "조번호")`

#### `tenant.l3_documents`  · 11컬럼 · 2행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `doc_id` | uuid | PK | `gen_random_uuid()` |
| `org_id` | uuid | NN · FK→orgs(org_id) | — |
| `원본파일명` | text | NN | — |
| `version` | text |  | — |
| `시행일` | date |  | — |
| `status` | text | NN | — |
| `extraction` | text | NN | `'native'::text` |
| `파싱품질` | text | NN | — |
| `dangling수` | integer | NN | `0` |
| `created_at` | timestamptz | NN | `now()` |
| `출처` | text | NN | `'기관업로드'::text` |

- CHECK `l3_documents_extraction_check` — `((extraction = ANY (ARRAY['native'::text, 'dedupe'::text, 'hancom'::text, 'vlm'::text, 'hwpx'::text, 'hwp'::text])))`
- CHECK `l3_documents_status_check` — `((status = ANY (ARRAY['active'::text, 'superseded'::text])))`
- CHECK `l3_documents_출처_check` — `(("출처" = ANY (ARRAY['기관업로드'::text, '테스트픽스처'::text])))`
- CHECK `l3_documents_파싱품질_check` — `(("파싱품질" = ANY (ARRAY['대기'::text, 'pass'::text, 'warn'::text, 'fail'::text])))`

#### `tenant.orgs`  · 6컬럼 · 2행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `org_id` | uuid | PK | `gen_random_uuid()` |
| `기관명` | text | NN | — |
| `사업명` | ARRAY | NN | `'{}'::text[]` |
| `created_at` | timestamptz | NN | `now()` |
| `주소` | text |  | — |
| `부서` | text |  | — |

#### `tenant.plan_tasks`  · 15컬럼 · 0행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `task_id` | bigint | PK | `nextval('plan_tasks_task_id_seq'::regclass)` |
| `org_id` | uuid | FK→orgs(org_id) | — |
| `plan_id` | bigint | FK→expense_plans(plan_id) | — |
| `decision_id` | bigint | FK→decisions(decision_id) | — |
| `출처` | text | NN | — |
| `코드` | text | FK→check_items(code) | — |
| `구분` | text | NN | — |
| `항목` | text | NN | — |
| `설명` | text |  | — |
| `due_date` | date |  | — |
| `날짜_사용자수정` | boolean | NN | `false` |
| `상태` | text | NN | `'준비필요'::text` |
| `created_at` | timestamptz | NN | `now()` |
| `updated_at` | timestamptz | NN | `now()` |
| `유형` | text | NN | `'기타'::text` |

- CHECK `plan_tasks_구분_check` — `(("구분" = ANY (ARRAY['결제전'::text, '결제후'::text, '집행'::text])))`
- CHECK `plan_tasks_상태_check` — `(("상태" = ANY (ARRAY['준비필요'::text, '집행예정'::text, '완료'::text])))`
- CHECK `plan_tasks_유형_check` — `(("유형" = ANY (ARRAY['기타'::text, '계약'::text, '비교견적'::text])))`
- CHECK `plan_tasks_출처_check` — `(("출처" = ANY (ARRAY['ai'::text, 'user'::text])))`

#### `tenant.unmapped_premise`  · 8컬럼 · 82행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `id` | bigint | PK | `nextval('unmapped_premise_id_seq'::regclass)` |
| `premise_text` | text | NN | — |
| `근거조항` | jsonb |  | — |
| `사업명` | text |  | — |
| `비목` | text |  | — |
| `발생횟수` | integer | NN | `1` |
| `최초` | timestamptz | NN | `now()` |
| `최근` | timestamptz | NN | `now()` |

- UNIQUE — `UNIQUE NULLS NOT DISTINCT (premise_text, "사업명", "비목")`


### 3-2. `corpus.*` — Agent 세션 소유 · **읽기 참조**

우리가 쓰는 것은 `check_items` 하나뿐이라 그것만 편다. 나머지는 판정 파이프라인의 표이고 구조를 바꾸자고 하지 않는다 — 여기 베껴 두면 사본이 곧 낡는다. 구조가 필요하면 DB 를 직접 조회해라.

#### `corpus.check_items`  · 12컬럼 · 52행

| 컬럼 | 타입 | | 기본값 |
|---|---|---|---|
| `code` | text | PK | — |
| `사업명` | text |  | — |
| `비목` | text |  | — |
| `구분` | text | NN | — |
| `항목` | text | NN | — |
| `설명` | text |  | — |
| `기본_오프셋일` | integer |  | — |
| `근거` | jsonb |  | — |
| `verified` | boolean | NN | `false` |
| `검수자` | text |  | — |
| `검수일` | date |  | — |
| `유형` | text | NN | `'기타'::text` |

- CHECK `check_items_구분_check` — `(("구분" = ANY (ARRAY['결제전'::text, '결제후'::text])))`
- CHECK `check_items_유형_check` — `(("유형" = ANY (ARRAY['기타'::text, '계약'::text, '비교견적'::text])))`
- CHECK `ck_check_items_오프셋부호` — `((("기본_오프셋일" IS NULL) OR (("구분" = '결제전'::text) AND ("기본_오프셋일" <= 0)) OR (("구분" = '결제후'::text) AND ("기본_오프셋일" >= 0))))`


## 4. 이 문서를 다시 만들 때 · 스키마를 대조할 때

| 도구 | 무엇을 하나 |
|---|---|
| `db/tools/gen_api_db_spec.py` | **이 문서 전체를 다시 만든다** (API + DB). 산문까지 이 스크립트 안에 있다 |
| `db/tools/dump_db_schema.py` | 살아있는 DB 스키마만 덤프한다. 이 문서와 **독립 교차검증**용 |

두 도구가 서로를 검증한다. 2026-09-01 대조에서 13개 표·CHECK 18개가 전부 일치했다.

### "스키마 대조"는 질문이 둘이다 — 섞으면 안 된다

| 질문 | 방법 | 답하는 것 |
|---|---|---|
| **빈 DB 로 `db/init` 을 돌리면 지금 DB 와 같은가** | 새 DB 를 만들어 `db/init/*.sql` 적용 후 덤프 대조 | "초기화 스크립트가 현재 상태를 재현한다" |
| **이미 있는 DB 에 `db/init` 을 다시 적용하면 최신이 되는가** | 기존 DB 에 적용 후 덤프 대조 | "기존 DB 가 갱신된다" |

⚠️ **앞의 질문에 통과해도 뒤의 질문은 미해결일 수 있다.** 실제로 2026-09-01 에
그랬다 — 그날 변경 둘(`check_items.유형` · `l3_documents.파싱품질='대기'`)이 전부
`CREATE TABLE IF NOT EXISTS` **안에 인라인**이고 `ALTER` 가 없어서, 빈 DB 는 최신으로
서는데 **기존 DB 는 영원히 갱신이 안 된다.** 빈 DB 로만 대조하고 안심하면 이걸 못 본다.


## 5. 이 문서가 다루지 않는 것

- **왜 그런가** — 계약 문서 `docs/7_백엔드/API_계약_v1.0.md` 를 봐라. 특히 §8 "만들지 않는
  것"과 §9 "실서버로 갈아끼울 때 버그로 오해할 것"은 이 문서에 대응물이 없다.
- **판정 품질 수치** — `docs/기록/2026-08-31_축별보고.md` 가 기준이다. 일치율을 인용할 때는 **다수결
  기준선을 반드시 병기**해야 한다(골든셋 정답이 "불가"로 쏠려 상수 예측기가 이미
  66.1% 를 낸다). 검색 지표(hit@k)는 그 편향과 무관하다.
- **eval 스키마** · 코퍼스 적재 절차 — Agent 세션 범위다.
