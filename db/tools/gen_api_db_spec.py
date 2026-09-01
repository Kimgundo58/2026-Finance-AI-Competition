# -*- coding: utf-8 -*-
"""`docs/7_백엔드/API_DB_명세_v1.md` 를 **실측에서 생성**한다. 손으로 베끼지 않는다.

    PYTHONIOENCODING=utf-8 python <이 파일> [--out API_DB_명세_v1.md]

왜 생성인가 — 계약이 이미 셋(모델·구현·문서)으로 갈라져 2026-09-01 에 결함이 났다.
넷째 사본을 손으로 만들면 같은 사고가 난다. API 는 `app.openapi()`(프론트가 실제로
보는 스펙), DB 는 `information_schema` + `pg_constraint` 실조회에서 뽑는다.

산문(0절·SSE·경계)은 이 스크립트 안의 상수다 — 그래야 재생성이 문서 전체를
복원한다. 스키마가 바뀌면 문서를 고치는 게 아니라 이걸 다시 돌린다.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve()
# 저장소 루트를 찾는다 (스크래치패드에서 돌아도 되게)
for 후보 in (Path.cwd(), *Path.cwd().parents):
    if (후보 / "server" / "main.py").exists():
        ROOT = 후보
        break
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# ════════════════════════════════════════════════════════════════════
# 산문 — 이 파일이 기준이다. 산출된 .md 를 손으로 고치면 다음 생성 때 사라진다
# ════════════════════════════════════════════════════════════════════

머리말 = """# API · DB 명세 v1

> 🔴 **이 문서는 「지금 실제로 무엇이 있는가」다.** 「왜 이렇게 주는가」(계약·근거·
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
> 생성 시각 `{생성시각}` · 소스 `server.main:app` · `{DSN요약}` · 생성기 `{생성기}`
"""

위치절 = """
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

계약 문서가 «만들지 않는 것»(§8)이라 못 박은 기능은 이 문서에 스키마가 있어도
만들지 않는다.
"""

SSE절 = """
## 2. SSE — OpenAPI 에 안 잡히는 부분

`/api/normalize` 와 `/api/judge` 는 `text/event-stream` 이라 OpenAPI 가 본문 스키마를
못 준다. 아래는 **계약 문서 §5·§6 의 인용**이고, `tests/test_contract.py` 가 순서와
키를 검증한다. 출처: `docs/7_백엔드/API_계약_v1.0.md`.

### `POST /api/judge` — 이벤트 8종, 이 순서

```
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

🔴 `저장: false` **는 실패가 아니다.** 아래 셋은 전부 정상 경로이고, 빨간 배너로
그리면 안 된다 — `plan_id 없음` · `캐시 적중 — 새 판정 기록 없음` ·
`decision_id 없음 — 판정이 기록되지 않았다`.

🔴 `결과` 이벤트에 `decision_id` 는 **실리지 않는다.** 캐시·응답에 박히면 다른 요청이
남의 판정 기록을 자기 계획에 가리키게 된다(TENANT_LEAK 류). 회귀 테스트로 잠가 뒀다.

`?목=가능|조건부|불가|판단불가` 로 4-way 화면을 전부 그려볼 수 있다.

### `POST /api/normalize`

```
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
"""

DB머리 = """
## 3. DB 스키마 (실조회)

### 소유와 경계

- **`tenant.*` 는 우리(프론트–백엔드) 소유다.** 읽고 쓴다.
- 🔴 **`corpus.*` 는 Agent 세션 소유다 — 이 문서에서는 «읽기 참조».**
  우리가 실제로 쓰는 것은 `check_items` 하나뿐이다(할일 코드·구분·유형·오프셋일).
  나머지는 판정 파이프라인이 쓰는 표이고, **여기서 구조를 바꾸자고 하면 안 된다.**
- `eval.*` 은 평가 전용이라 이 문서 범위 밖이다.

표기 — `NN` = NOT NULL · `PK` = 기본키 · `FK→` = 외래키 · `CHECK` 는 표 아래 별도.

⚠️ **행수는 재생성 시점의 스냅샷이다 — 계약이 아니다.** 특히 `expense_plans` 의
현재 행은 테스트 픽스처 찌꺼기가 섞여 있어 실사용 데이터가 아니다. 컬럼·타입·제약만
믿어라.
"""

재현절차 = """
## 4. 이 문서를 다시 만들 때 · 스키마를 대조할 때

| 도구 | 무엇을 하나 |
|---|---|
| `db/tools/gen_api_db_spec.py` | **이 문서 전체를 다시 만든다** (API + DB). 산문까지 이 스크립트 안에 있다 |
| `db/tools/dump_db_schema.py` | 살아있는 DB 스키마만 덤프한다. 이 문서와 **독립 교차검증**용 |

두 도구가 서로를 검증한다. 2026-09-01 대조에서 13개 표·CHECK 18개가 전부 일치했다.

### 🔴 「스키마 대조」는 질문이 둘이다 — 섞으면 안 된다

| 질문 | 방법 | 답하는 것 |
|---|---|---|
| **빈 DB 로 `db/init` 을 돌리면 지금 DB 와 같은가** | 새 DB 를 만들어 `db/init/*.sql` 적용 후 덤프 대조 | 「초기화 스크립트가 현재 상태를 재현한다」 |
| **이미 있는 DB 에 `db/init` 을 다시 적용하면 최신이 되는가** | 기존 DB 에 적용 후 덤프 대조 | 「기존 DB 가 갱신된다」 |

⚠️ **앞의 질문에 통과해도 뒤의 질문은 미해결일 수 있다.** 실제로 2026-09-01 에
그랬다 — 그날 변경 둘(`check_items.유형` · `l3_documents.파싱품질='대기'`)이 전부
`CREATE TABLE IF NOT EXISTS` **안에 인라인**이고 `ALTER` 가 없어서, 빈 DB 는 최신으로
서는데 **기존 DB 는 영원히 갱신이 안 된다.** 빈 DB 로만 대조하고 안심하면 이걸 못 본다.
"""

꼬리 = """
## 5. 이 문서가 다루지 않는 것

- **왜 그런가** — 계약 문서 `docs/7_백엔드/API_계약_v1.0.md` 를 봐라. 특히 §8 「만들지 않는
  것」과 §9 「실서버로 갈아끼울 때 버그로 오해할 것」은 이 문서에 대응물이 없다.
- **판정 품질 수치** — `docs/기록/2026-08-31_축별보고.md` 가 기준이다. 일치율을 인용할 때는 **다수결
  기준선을 반드시 병기**해야 한다(골든셋 정답이 「불가」로 쏠려 상수 예측기가 이미
  66.1% 를 낸다). 검색 지표(hit@k)는 그 편향과 무관하다.
- **eval 스키마** · 코퍼스 적재 절차 — Agent 세션 범위다.
"""


# ════════════════════════════════════════════════════════════════════
# API — app.openapi() 에서 뽑는다
# ════════════════════════════════════════════════════════════════════

def _주석(t) -> str:
    """타입 주석을 사람이 읽는 문자열로. 🔴 OpenAPI 이름은 쓰지 않는다.

    FastAPI 는 한글 클래스명을 `server__models________4` 로 뭉갠다(비ASCII 를 `_` 로
    치환). 그 이름을 그대로 문서에 실으면 프론트가 스키마를 못 찾는다. 그래서
    **라우트 객체에 달린 실제 pydantic 클래스**에서 이름과 필드를 뽑는다.
    """
    import typing
    from pydantic import BaseModel

    if t is None or t is type(None):
        return "null"
    if isinstance(t, type) and issubclass(t, BaseModel):
        return f"`{t.__name__}`"
    origin = typing.get_origin(t)
    인자 = typing.get_args(t)
    if origin is typing.Literal:
        return " \| ".join(f"`{v}`" for v in 인자)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        갈래 = [_주석(a) for a in 인자]
        널 = "null" in 갈래
        갈래 = [g for g in 갈래 if g != "null"]
        return " \| ".join(갈래) + (" \| null" if 널 else "")
    if origin in (list, set, tuple):
        return f"{_주석(인자[0]) if 인자 else 'any'}[]"
    if origin is dict:
        return f"dict[{_주석(인자[0])}, {_주석(인자[1])}]" if 인자 else "object"
    if isinstance(t, type):
        return {"str": "str", "int": "int", "float": "float", "bool": "bool",
                "NoneType": "null"}.get(t.__name__, t.__name__)
    return str(t).replace("typing.", "")


def _모델수집(t, 모음: list) -> None:
    """타입 안에 든 pydantic 모델을 (상속 부모까지) 전부 모은다."""
    import typing
    from pydantic import BaseModel

    if isinstance(t, type) and issubclass(t, BaseModel):
        if t not in 모음:
            모음.append(t)
            for 부모 in t.__mro__[1:]:
                if isinstance(부모, type) and issubclass(부모, BaseModel) \
                        and 부모 is not BaseModel:
                    _모델수집(부모, 모음)
            for f in t.model_fields.values():
                _모델수집(f.annotation, 모음)
        return
    for a in typing.get_args(t):
        _모델수집(a, 모음)


_SSE경로 = {"/api/normalize", "/api/judge"}


def _라우트들(app):
    """`app.routes` 만 보면 안 된다 — 이 FastAPI 버전의 `include_router` 는 라우트를
    평탄화하지 않고 `fastapi.routing._IncludedRouter` 객체 하나만 넣는다(지연 결합).
    그래서 하위 라우터를 직접 편다."""
    from fastapi.routing import APIRoute
    from server import routes_l3, routes_plans, routes_tasks

    out = []
    for r in list(app.routes):
        if isinstance(r, APIRoute):
            out.append(r)
    for 라우터 in (routes_plans.router, routes_tasks.router, routes_l3.router):
        for r in 라우터.routes:
            if isinstance(r, APIRoute):
                out.append(r)
    return out


def api절(app) -> tuple[str, int, int]:
    스펙 = app.openapi()
    경로들 = 스펙["paths"]
    라우트 = _라우트들(app)

    줄 = ["", "## 1. API — 실측 (`app.openapi()` + 라우트 객체)", "",
          "### 1-1. 엔드포인트", "",
          "🔴 `text/event-stream` 인 둘은 응답 본문이 SSE 라 스키마가 없다 — §2 를 봐라.",
          "",
          "| 메서드 | 경로 | 요청 | 응답(2xx) |", "|---|---|---|---|"]

    모델: list = []
    표행: dict[tuple, tuple] = {}
    for r in 라우트:
        요청 = "—"
        bf = getattr(r, "body_field", None)
        if bf is not None:
            # 🔴 이 FastAPI 버전은 `ModelField.type_` 이 None 이다 — 실제 클래스는
            #    `field_info.annotation` 에 있다. `type_` 만 보면 전부 null 로 나온다.
            t = getattr(getattr(bf, "field_info", None), "annotation", None)                 or getattr(bf, "type_", None)
            if t is not None and getattr(t, "__name__", "").startswith("Body_"):
                # multipart 폼 — 동적 생성 모델이라 필드를 직접 편다
                필드 = ", ".join(f"`{k}`" for k in t.model_fields)
                요청 = f"multipart ({필드})"
            else:
                요청 = _주석(t)
                _모델수집(t, 모델)
        응답 = "—"
        if getattr(r, "response_model", None) is not None:
            응답 = _주석(r.response_model)
            _모델수집(r.response_model, 모델)
        elif r.path in _SSE경로:
            # `response_class` 로는 못 가른다 — 핸들러가 StreamingResponse 를 «반환» 할 뿐
            # 라우트의 기본 응답 클래스는 JSONResponse 그대로다.
            응답 = "**SSE** — §2"
        else:
            응답 = "object (모델 없음)"
        코드 = getattr(r, "status_code", None)
        if 코드 and 코드 != 200:
            응답 += f" ({코드})"
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            표행[(r.path, m)] = (요청, 응답)

    # 🔴 OpenAPI 가 기준이다 — 라우트 객체로 못 본 경로가 있으면 그것도 싣는다
    메서드수 = 0
    for 경로 in sorted(경로들):
        for 메서드 in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            if 메서드.lower() not in 경로들[경로]:
                continue
            메서드수 += 1
            요청, 응답 = 표행.get((경로, 메서드), ("?", "?  ← 라우트 객체에서 못 찾음"))
            줄.append(f"| {메서드} | `{경로}` | {요청} | {응답} |")

    # ── 1-2. 쿼리·경로 파라미터 — 목록·필터를 부르려면 이게 있어야 한다 ──
    줄 += ["", "### 1-2. 쿼리 · 경로 파라미터", "",
           "OpenAPI `parameters` 실측. `기본값` 이 있으면 안 보내도 된다.", ""]
    for 경로 in sorted(경로들):
        for 메서드 in ("get", "post", "put", "patch", "delete"):
            op = 경로들[경로].get(메서드)
            if not op or not op.get("parameters"):
                continue
            줄 += [f"**`{메서드.upper()} {경로}`**", "",
                   "| 이름 | 위치 | 타입 | 필수 | 기본값 | 설명 |", "|---|---|---|:--:|---|---|"]
            for prm in op["parameters"]:
                sch = prm.get("schema", {})
                기본 = sch.get("default", None)
                갈래 = sch.get("anyOf") or [sch]
                타입 = " \| ".join(
                    {"string": "str", "integer": "int", "number": "float",
                     "boolean": "bool", "null": "null"}.get(g.get("type", ""), g.get("type", "any"))
                    for g in 갈래)
                줄.append(f"| `{prm['name']}` | {prm.get('in', '')} | {타입} | "
                          f"{'✅' if prm.get('required') else ''} | "
                          f"{f'`{기본!r}`' if 기본 is not None else '—'} | "
                          f"{(prm.get('description') or '')[:70]} |")
            줄.append("")

    줄 += ["", "### 1-3. 요청·응답 스키마", "",
           "`server/models.py` 의 pydantic 모델에서 직접 뽑았다. "
           "필수 = 기본값이 없는 필드.", ""]

    for M in sorted(모델, key=lambda m: m.__name__):
        줄 += [f"**`{M.__name__}`**", "", "| 필드 | 타입 | 필수 | 기본값 |", "|---|---|:--:|---|"]
        for 이름, f in M.model_fields.items():
            필수 = f.is_required()
            기본 = "—"
            if not 필수:
                try:
                    v = f.get_default(call_default_factory=True)
                    기본 = "—" if v is None else f"`{v!r}`"
                except Exception:                            # noqa: BLE001
                    기본 = "—"
            줄.append(f"| `{이름}` | {_주석(f.annotation)} | {'✅' if 필수 else ''} | {기본} |")
        줄.append("")
    return "\n".join(줄), len(경로들), 메서드수


# ════════════════════════════════════════════════════════════════════
# DB — information_schema + pg_constraint
# ════════════════════════════════════════════════════════════════════

_컬럼_Q = """
SELECT column_name, data_type, character_maximum_length, numeric_precision,
       numeric_scale, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s
ORDER BY ordinal_position
"""

_제약_Q = """
SELECT c.conname, c.contype, pg_get_constraintdef(c.oid)
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = %s AND t.relname = %s
ORDER BY c.contype, c.conname
"""


def _타입표기(행) -> str:
    이름, 타입, 길이, 정밀, 소수, _, _ = 행
    if 타입 == "character varying":
        return f"varchar({길이})" if 길이 else "varchar"
    if 타입 == "numeric" and 정밀:
        return f"numeric({정밀},{소수})"
    return {"timestamp with time zone": "timestamptz",
            "timestamp without time zone": "timestamp",
            "double precision": "float8",
            "character": "char"}.get(타입, 타입)


def db절() -> str:
    import psycopg

    줄 = [DB머리]
    with psycopg.connect(DSN, connect_timeout=5) as conn:
        표 = conn.execute("""
            SELECT table_schema, table_name FROM information_schema.tables
            WHERE table_schema IN ('tenant','corpus') AND table_type='BASE TABLE'
            ORDER BY table_schema, table_name""").fetchall()

        # 🔴 corpus 는 `check_items` 만 편다. 나머지는 목록만 — 남의 표를 여기 베껴
        #    두면 그 사본이 곧 낡고, 낡은 사본은 «있다» 는 착각을 만든다.
        펼침 = {("corpus", "check_items")}

        for 스키마, 제목, 주 in (("tenant", "### 3-1. `tenant.*` — 우리 소유 (읽기·쓰기)", ""),
                               ("corpus", "### 3-2. `corpus.*` — 🔴 Agent 세션 소유 · **읽기 참조**",
                                "우리가 쓰는 것은 `check_items` 하나뿐이라 그것만 편다. "
                                "나머지는 판정 파이프라인의 표이고 구조를 바꾸자고 하지 "
                                "않는다 — 여기 베껴 두면 사본이 곧 낡는다. 구조가 필요하면 "
                                "DB 를 직접 조회해라.")):
            줄 += ["", 제목, ""]
            if 주:
                줄 += [주, ""]
            나머지 = []
            for s, t in [(a, b) for a, b in 표 if a == 스키마]:
                if s == "corpus" and (s, t) not in 펼침:
                    n = conn.execute(f'SELECT count(*) FROM "{s}"."{t}"').fetchone()[0]
                    c = conn.execute(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name=%s", (s, t)).fetchone()[0]
                    나머지.append(f"| `{s}.{t}` | {c} | {n:,} |")
                    continue
                컬럼 = conn.execute(_컬럼_Q, (s, t)).fetchall()
                제약 = conn.execute(_제약_Q, (s, t)).fetchall()
                행수 = conn.execute(f'SELECT count(*) FROM "{s}"."{t}"').fetchone()[0]

                pk, fk, uq, ck = set(), {}, [], []
                for 이름, 종류, 정의 in 제약:
                    if 종류 == "p":
                        pk |= {c.strip().strip('"')
                               for c in 정의[정의.find("(") + 1:정의.rfind(")")].split(",")}
                    elif 종류 == "f":
                        컬 = 정의[정의.find("(") + 1:정의.find(")")].strip().strip('"')
                        대상 = 정의.split("REFERENCES", 1)[1].strip()
                        fk[컬] = 대상.split(" ON ")[0].strip()
                    elif 종류 == "u":
                        uq.append(정의)
                    elif 종류 == "c":
                        ck.append((이름, 정의))

                줄 += [f"#### `{s}.{t}`  · {len(컬럼)}컬럼 · {행수}행", "",
                       "| 컬럼 | 타입 | | 기본값 |", "|---|---|---|---|"]
                for 행 in 컬럼:
                    이름 = 행[0]
                    표식 = []
                    if 이름 in pk:
                        표식.append("PK")
                    if 행[5] == "NO" and 이름 not in pk:
                        표식.append("NN")
                    if 이름 in fk:
                        표식.append(f"FK→{fk[이름]}")
                    기본 = 행[6]
                    기본 = f"`{기본}`" if 기본 else "—"
                    줄.append(f"| `{이름}` | {_타입표기(행)} | {' · '.join(표식)} | {기본} |")
                줄.append("")
                for 이름, 정의 in ck:
                    줄.append(f"- CHECK `{이름}` — `{정의[정의.find('('):]}`")
                for 정의 in uq:
                    줄.append(f"- UNIQUE — `{정의}`")
                if ck or uq:
                    줄.append("")
    return "\n".join(줄)


# ════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "7_백엔드" / "API_DB_명세_v1.md"))
    args = ap.parse_args()

    from server.main import app

    본문_api, 경로수, 메서드수 = api절(app)
    본문_db = db절()

    문서 = "\n".join([
        머리말.format(생성시각=datetime.now(timezone.utc).astimezone()
                    .isoformat(timespec="seconds"),
                    DSN요약=DSN.rsplit("@", 1)[-1],
                    생성기=Path(__file__).resolve().relative_to(ROOT).as_posix()),
        위치절,
        본문_api,
        SSE절,
        본문_db,
        재현절차,
        꼬리,
    ])
    Path(args.out).write_text(문서, encoding="utf-8")
    print(f"썼다: {args.out}  ({len(문서.splitlines())}행 · "
          f"API {경로수}경로/{메서드수}메서드)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
