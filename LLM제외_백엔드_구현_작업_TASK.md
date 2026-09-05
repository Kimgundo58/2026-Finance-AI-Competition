# LLM 제외 — 백엔드 구현 작업 TASK

> **이 문서 하나만 읽고 시작하면 된다.** 2026-09-05 오너 확정 스코프.
> 판정(LLM) 쪽은 오너가 따로 잡고 있다. 여기 없는 것은 네 일이 아니다.

```
클론
 └ git checkout -b work/백엔드-0905 origin/main
 └ §2 「건드리면 안 되는 파일」 확인      ← 여기만 지키면 오너와 안 겹친다
 └ §4 A → B → D 순으로 구현
 └ PR 로 올린다. main 직접 머지 금지
```

## 1. 범위

| | 무엇 | 이번 라운드 |
|---|---|---|
| A | F축 데이터 계층 정상화 | **구현** |
| B | L3 dangling 상세 | **구현** |
| D | 프론트–백엔드 갭 (AI CHAT 제외) | **구현** |
| C | 인증 / IDOR (`org_id` 자기신고) | 🔴 **제외 — 손대지 마라** |
| F | 운영 자동화 (GPU watchdog · `/api/gpu/reap`) | 🔴 **제외** |
| — | 화면 12 AI CHAT | 🔴 **이번엔 안 만든다** |
| — | 판정 정확도 · 검색 · 프롬프트 | 🔴 **오너 소관** |

## 2. 🔴 건드리면 안 되는 파일

오너가 같은 기간에 이 파일들을 고치고 있다. 만지면 충돌하거나, 더 나쁘게는
**오너의 판정 실험 조건이 조용히 바뀐다**(기준선 비교가 깨진다).

```
scripts/orchestrate.py      판정 흐름의 중심
scripts/assemble_context.py 프롬프트 B0 — 🔴 여기 한 글자가 바뀌면 그날 run 이 무효다
scripts/llm_validate.py     검증·강등
scripts/llm_schema.py       판정 출력 스키마
scripts/normalize_run.py    ① 정규화 LLM 호출
scripts/retrieve.py         검색
scripts/rule_lookup.py      룰 조회
scripts/adapter.py          모델 슬롯 — 🔴 A3 에서 «읽기만» 한다
scripts/eval_e2e.py         정답셋 평가 실행기
scripts/eval_store.py       평가 결과 저장
```

**네 것**: `server/**` · `db/init/**` · `scripts/l3_parse.py` · `scripts/archive/cli/**` ·
`tests/**` · `docs/7_백엔드/**` · `docs/9-2_백엔드_프론트_미결.md`

**공유 — 고치기 전에 오너에게 말한다**: `CLAUDE.md` · `docs/0_현황.md` · `docs/9_미결.md`

🔴 **DB 스키마를 바꾸면 PR 본문에 반드시 적는다.** 오너의 판정 파이프라인이 같은 DB 를 읽는다.

## 3. 기동

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
docker compose up -d db                              # 로컬 Postgres
python -m server.main --selftest                     # 서버 안 띄우고 계약 검증
uvicorn server.main:app --reload --port 8000
```

`MOCK=1` 이면 목 응답이 나간다. 🔴 **목이 통과해도 실 경로는 별개다** — 2026-09-03 에
목은 `인용` 을, 실 경로는 `인용목록` 을 돌려줘서 테스트가 전부 초록인 채 실서버 판정
전수가 근거 없이 나갔다. 고칠 때마다 **`MOCK=0` 으로 한 번 더** 확인한다.

## 4. 작업

### A. F축 데이터 계층 — 전부 정상화

| # | 지금 상태 (실측 2026-09-05) | 해야 할 것 | 확인 |
|---|---|---|---|
| A1 | `server/main.py:1250` `_실_프로필_저장()` 이 **스텁**이다 — `{"저장": False, "이유": "f_profile 쓰기 경로 미배선"}` 만 반환. 주석은 "E 세션이 소유한다"인데 그 세션은 끝났다 | `tenant.f_profile` 실제 UPSERT (`org_id`·사업명·`협약시작일`·`협약종료일`·`정부지원_현금`·`자기부담_현금`). RLS/GUC 패턴은 `server/routes_plans.py` 의 `_질의_저장`/`_계획_주인` 을 그대로 따른다 | `PUT /api/profile` → `저장==True` · 재조회로 값 확인 |
| A2 | `tenant.f_exec`(F3)·`tenant.f_personnel`(F4) 가 **`server/` 전체에서 참조 0건**. `main.py:1246` `_실_프로필()` 이 `"f3": [], "f4": []` 로 **하드코딩**한다 (스키마는 `db/init/01_schema.sql:424·439` 에 이미 있고 RLS 정책도 `:515·517` 에 있다) | 빈 배열부터 걷어내고 실쿼리를 붙인다. 쓰기 경로도 A1 과 같은 자리에서 | `GET /api/profile` 이 실제 행을 돌려주는가 |
| A3 | 협약서 파싱 통로가 **없다**. `scripts/adapter.py:81` 의 `"F문서파싱"` 슬롯은 **선언만** 있고 호출부가 코드베이스 어디에도 없다 (`:392` 는 슬롯 목록 순회) | 협약서 업로드 → **분리 적재**: 특약조항은 L3(`tenant.l3_articles`), 협약기간·금액은 F1(`tenant.f_profile`). 엔드포인트 본체는 **`server/` 에 만든다** | 협약서 1건 올려 양쪽 테이블에 각각 들어가는지 |
| A4 | `server/main.py:1405` selftest 가 `PUT /api/profile` 의 **`status_code == 200` 만** 본다. `"저장"` 필드는 **안 본다** | `저장 == True` 로 강화. **A1 과 같은 커밋에서 한다** | 안 하면 「그린인데 저장 안 되는」 상태가 그대로 재발한다 |

🔴 **A3 에서 `scripts/adapter.py` 를 수정하지 마라.** 오너 소유다(§2). 슬롯의 규약을
**읽어서** 맞춰 호출만 한다. 설계 근거는 `docs/부록/도메인_배경_전문.md:147-149`.

### B. L3 dangling 상세

| # | 지금 상태 | 해야 할 것 |
|---|---|---|
| B1 | `server/routes_l3.py:299` 주석이 그대로 말한다 — "dangling 상세(조·참조·사유)는 **저장할 테이블이 아직 없다** — 자리만 두고 비워 둔다". 그래서 `:361` 응답의 `dangling=[]` 이 **항상 빈 배열**이다. 건수만 `tenant.l3_documents.dangling수` 에 있다(`scripts/l3_parse.py:78`) | ⑴ 상세 테이블 신설 ⑵ `scripts/l3_parse.py:132` 의 dangling 카운트 자리에서 (조·참조·사유)를 같이 적재 ⑶ `GET /api/l3/{doc_id}` 응답에 실제로 싣는다 |

🔴 **끊긴 참조는 판정 시점이 아니라 «업로드 시점»에 알린다** (`CLAUDE.md`). 이 항목의 존재 이유다.

### D. 프론트–백엔드 갭 — AI CHAT «만» 제외

| # | 화면 | 지금 상태 | 해야 할 것 |
|---|---|---|---|
| D1 | 13 집행일정 | 프론트가 `localStorage`(`checkumait-clean-schedules-v2`)에만 저장한다. 서버엔 `server/routes_tasks.py` 의 `plan_tasks`(+`due_date`, `:195` 의 `일정만` 필터)가 **이미 있다** | 🔴 **새 테이블부터 만들지 마라.** `plan_tasks` 로 덮이는지 먼저 대조하고, 모자란 필드만 더한다 |
| D2 | 9 지급수수료 | `server/main.py:627` `/api/vocab` 이 비목 **10종**만 준다. 프론트는 지급수수료 **하위 16종** 배열을 기대한다. 별칭은 `corpus.item_vocab.별칭` 에서 이미 나간다 | 하위 세목을 실을 자리를 만든다. 🔴 **`비목` 10종 자체를 늘리지 마라** — `guided_json_enum` 이 그 10종에 묶여 있어 판정이 깨진다. **하위 세목은 별도 키로** |
| D3 | 4 업로드 | 프론트 `accept` 가 `.doc/.docx` 까지 받는데 서버는 415 로 거부한다(`server/routes_l3.py:34` `허용_확장자 = {"pdf","hwpx","hwp"}` · `:129`). **서버가 맞다** — DOC/DOCX 파서가 없고 만들지 않는다 | 프론트 `accept` 정리 또는 안내 메시지. 서버는 그대로 |
| D4 | 7 근거 | 🔴 **백엔드는 이미 다 준다** (`server/main.py:1203` 이 `인용`/`인용목록` 양쪽을 받는다). 프론트 저장소는 **이 repo 에 없다** | 「구현」이 아니라 **「프론트에서 실제로 그려지는지 확인」** 이다 |

### E. 잡음

| # | 지금 상태 | 해야 할 것 |
|---|---|---|
| E1 | 팟 대장 경로가 갈렸다 — `scripts/archive/cli/runpod_pod.py` 는 `scripts/archive/.claude/_runpod_open.json` 에 쓰고, `.claude/hooks/guard_gpu.py` 는 루트 `.claude/_runpod_open.json` 을 읽는다 | 경로 통일. 열 때 훅이 못 보고, 닫을 때 유령 항목으로 계속 운다 |
| E2 | `dry` 경로에서 `subprocess` 읽기 스레드가 cp949 로 디코딩하다 `UnicodeDecodeError` | 인코딩 명시 |

## 5. 열려 있는데 «이번엔 안 고치는 것»

지워두면 다음 사람이 또 찾아낸다. 상태만 남긴다.

```
C. org_id 가 인증이 아니라 «자기신고» 다 — 갈래가 셋이고 섞으면 안 된다
 └ 앱 층      «닫힘»   server/auth.py 의 OrgId주입 · routes_l3.py · routes_plans.py ·
                       main.py:769 — 전부 토큰이 이긴다
 └ DB 층(RLS) «열림»   tenant 11개 표 relforcerowsecurity=false ·
                       앱이 붙는 postgres 롤은 rolbypassrls=true → FORCE 를 켜도 우회된다
 └ 회귀검증   «미실시»  정상 경로가 안 깨졌는지 아직 안 쟀다
 🔴 「인증 설계가 선행조건」이라는 옛 서술은 지금 상태와 다르다 — 앱 층은 이미 닫혔다
 🔴 화면 3(기관 선택) 목록 API 가 org_id 를 실어 내보내는 순간 남은 방어가 사라진다
```

```
BE1  게스트를 expense_plans 에 무는 방법   org_id 가 uuid+FK 라 guest_<hex> 가 타입에서 거부
미결 #18  데모계정 — tenant.accounts 는 3행(demo·prototype·test). 「0행」은 낡은 서술
미결 #19  게스트 쓰기 RLS
미결 #20  SUDDOE_ORG_PARAM=0
미결 #21  목 서버
미결 #22 / BE2  AI 챗봇(화면 12) — 프론트 실물도 목이다(setTimeout 1200ms 고정응답,
                네트워크 호출 0건). UI 는 있고 두뇌가 없다
F    GPU watchdog 무인정지 · Cloud Scheduler ↔ /api/gpu/reap
```

상세는 `docs/9-2_백엔드_프론트_미결.md` · `docs/9_미결.md`. 충돌하면 그쪽이 정본이다.

## 6. 보고 · 커밋

```
브랜치   work/백엔드-0905      main 직접 커밋·머지 금지. PR 로만
커밋     실제 포함 파일만 메시지에 적는다 — git show --stat 목록과 일치시킨다
         🔴 2026-09-05 에 메시지가 부풀려져 문서 수정 3벌이 «들어간 줄 알고» 소실됐다
테스트   pytest . && python -m server.main --selftest
         MOCK=0 으로 한 번 더 — 목 통과는 실 경로의 증거가 아니다
PR 본문  DB 스키마 변경이 있으면 «반드시» 적는다 (오너 파이프라인이 같은 DB 를 읽는다)
```

🔴 **막히면 오너에게 묻는다.** 특히 §2 파일을 고쳐야 할 것 같을 때 — 거기서 임의로
진행하면 오너의 판정 실험이 조용히 무효가 된다.
