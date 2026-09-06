# 레인 L3 — 적재 API + 주간 스케줄러 설계

산출 코드: `server/routes_admin_ingest.py`(신규) + `server/main.py`(라우터 등록 2줄).
DB 쓰기는 «설계·코드만» — 실제 DDL(`status` CHECK 에 `staged` 추가)은 중앙이 친다.
스케줄러는 **등록하지 않았다** — 엔드포인트와 Cloud Scheduler 설정 문구까지다.

## 0. 오너 결정(2026-09-06, ai-8c 전달) 반영

```
새 테이블 없음. corpus.documents·doc_articles·chunks 에 «그대로» 넣되 status='staged'.
검수 -> staged→active 한 줄 UPDATE(같은 순간 옛 판 active→superseded, 이 파일 범위 밖).
근거: scripts/retrieve.py:41 필터가 이미 status='active' 를 건다 — staged 는
      «구조적으로» 판정 검색에 안 보인다. 새는 자리가 원천적으로 없다.
```

## 1. 이 레인이 «정한» 두 가지 (오너가 위임한 판단)

### ① parse_quality 어휘 — 새로 안 만든다

`corpus.documents`·`chunks` 의 `parse_quality` CHECK 는 이미 `high|low` 뿐이고
`retrieve.py:41` 이 문자열 `'high'` 를 정확히 문다. L3 의 `파싱품질`(대기/pass/warn/fail)
은 **다른 테이블**(`tenant.l3_documents`)의 다른 칸이라 안 섞인다 — 여기에 pass/warn/fail
을 또 만들면 "두 어휘가 같은 개념을 다르게 부른다"는 혼선만 는다.

그래서 이 파일의 **fail/warn/pass 라우팅은 저장하지 않는 «API 응답 값»** 이고,
저장되는 건 그중 두 값(warn→low, pass→high)뿐이다:

```
fail  = corpus 에 «아예 안 들어간다» → 저장할 칸이 필요 없다. 서버 로그 + API 응답이 기록의 전부
warn  = parse_quality='low' 로 들어간다  (TASK §7-G3 다섯 규칙 중 하나라도 걸림)
pass  = parse_quality='high' 로 들어간다  (다섯 규칙 전부 통과)
```

### ② embedding 시점 — 승인 시 생성(ai-8c 의견에 동의)

staged 행은 `retrieve.py:41` 필터(`status='active'`)를 어차피 못 지나가므로 staged
시점에 embedding 을 만들어도 승인 전까지는 **아무 데도 안 쓰인다** — 순수 비용이다.
반대로 검수 중 표가 밀린 게 드러나 재적재하면 그 embedding 은 버려진다(이중 낭비).
그래서 `chunks.embedding` 은 **NULL 로 남긴다.** 승인 엔드포인트(범위 밖)가
`staged→active` 로 올리는 순간 임베딩 배치를 태우는 게 다음 자리다.

## 2. `POST /admin/ingest` — 파이프라인

```
0) index_guard.reject_reason(src_path, layer)                — 자리 자체가 맞는지
     거부되면 그 자체로 fail (품질 문제 이전에 판정 인덱스 밖)
1) stage0_extract.extract(path)                               — 기존 추출기. 새로 안 짠다
     xml → 이미 조 단위. 그 외 → 본문 + 추출_품질_점검() → stage0_articles.split_articles()
2) parse_quality_판정(조목록, extraction, 표_문서, 목차_일치)   — TASK §7-G3 다섯 규칙
     조 0개 · 조번호 비단조 · extraction='vlm' · 표행 3줄 미만 · 목차 불일치
     -> high|low. 하나도 못 뽑았으면(조 0개+글자수 임계 미만) 그 앞단에서 이미 fail
3) INSERT documents·doc_articles·chunks (status='staged', index_target=false,
   embedding=NULL) — 재적재 대비 해당 doc_id 의 옛 조·청크는 지우고 다시 넣는다
4) recheck_queue 연계 — agent_a4(조_읽기·조_매칭·판정_변경·영향_레코드·적재)를 «그대로»
   재사용한다. 같은 규범군(family)의 현재 active 문서와 조 단위로 대조해
   BASIS_AMENDED·BASIS_RENUMBERED·BASIS_DELETED·ITEM_VOCAB_RECHECK 를 올린다.
   최초 적재(같은 family 의 active 가 없음)면 조용히 스킵 — 비교 대상이 없다.
```

`dry=true` 를 주면 0)~2) 만 하고 아무것도 안 쓴다 — 사람이 "이 파일을 넣으면 어떻게
판정될지" 미리 볼 수 있다.

## 3. `GET /admin/parse_report` — G4

`parse_quality='low'` 인 `documents` 행을 나열한다. **fail 은 여기 안 나온다** —
fail 은 애초에 안 들어가서 보여줄 행이 없다. fail 이력 테이블은 지금 없다(TASK 문서
어디에도 명세가 없어 지어내지 않았다) — 필요해지면 별도 요청으로 만든다.

## 4. `POST /admin/ingest/weekly` — 스케줄러가 칠 자리

`server/gpu_watchdog.py:gpu_reap()` 과 «같은 자리»다 — 1회 실행 엔드포인트를 먼저
굳히고, 실제 Scheduler 등록은 오너 승인 뒤 중앙이 한다.

```
🔴 지금은 미배선이다. `_수집_대기_목록()` 이 스텁 — G1(ai-53 발행처별 수집기)이
   아직 없어서 `_l3_수집_대기/*.json`(파일 하나 = 적재요청 1건) 을 로컬에서 읽는
   임시 다리만 놨다. 디렉터리가 없으면 빈 배치로 끝난다(고장 아님).
   G1 이 실제 수집 결과를 주기 시작하면 이 스텁 함수만 바꾸면 된다 — 한 건씩
   `admin_ingest()` 를 부르는 나머지 로직은 이미 돈다.
```

### Cloud Scheduler 설정 (문서만 — «등록은 안 했다»)

```yaml
# 🔴 오너 승인 후 중앙이 gcloud scheduler jobs create http 로 직접 등록한다.
# 여기 문구를 «그대로» 쓰면 된다 — 타임존 함정(D-day 하루 갈림과 같은 유형)을
# schedule 자체가 아니라 반드시 --time-zone 으로 명시해서 막는다.
name: l3-ingest-weekly
schedule: "0 10 * * 1"        # 매주 월요일 10:00 — 이 숫자는 --time-zone 이 있어야 KST 다
time-zone: "Asia/Seoul"        # 🔴 DB 는 Etc/UTC. 이 줄이 없으면 10시가 UTC 로 해석돼
                                #    한국 시각 «오후 7시» 에 돈다(9시간 차이) — 오늘 낮에
                                #    겪은 D-day 하루 갈림과 같은 함정 계열이다.
uri: https://<cloud-run-url>/admin/ingest/weekly
http-method: POST
headers:
  X-Admin-Token: "${SUDDOE_ADMIN_TOKEN}"    # 🔴 시크릿 매니저 참조로 넣는다 — 평문 금지
attempt-deadline: "1800s"       # 30분 — 배치라 gpu_reap(5분 주기)보다 넉넉히 잡는다
```

## 5. 안 한 것 (범위 밖 — 다음 레인 자리)

```
· staged → active 승인 엔드포인트(그 순간 구판 active→superseded 전환 포함)
· G1 발행처별 수집기(ai-53) — _수집_대기_목록() 스텁이 기다리는 자리
· G5 HWP 자동변환이 손변환을 대체해도 되는지 검산(LibreOffice+H2Orestart, 선행조사 완료됨)
· chunks 를 stage2_chunk.py 수준(토큰 길이 기준 분할·병합)으로 만드는 것 — 지금은
  1조=1청크 단순 매핑이다. staged 검수 단계(표·조가 제대로 들어왔나 보는 자리)엔
  충분하지만, active 승격 시 그 모듈로 다시 쪼개 넣는 게 다음 자리다
```

## 6. 회귀 확인

`pytest tests/` 306 passed · 3 xfailed(변경 전과 동일) — 새로 깨진 것 없음.
`parse_quality_판정()` 4개 케이스(정상/조0개/vlm/비단조) 직접 실행해 규칙대로 갈리는 것
확인. `index_guard.reject_reason()`·`agent_a4.family()` import·호출 확인.
`TestClient` 로 `/admin/ingest`·`/admin/parse_report` 가 토큰 없이 403(등록 확인, 다른
`/admin/*` 과 동일 규칙) 나는 것 확인 — 라이브 DB 없이도 여기까지는 실측했다.
🔴 **DB 쓰기 실경로(INSERT 3종+recheck_queue)는 라이브 DB 에 실제 태워보지 «않았다»**
(이 세션은 읽기전용 의무). `status='staged'` DDL 이 없는 지금 상태로 태우면
`documents_status_check` 위반으로 죽는 게 «맞는» 동작이다 — 중앙이 DDL 을 친 뒤
`dry=false` 로 실제 실행 검증을 한 번 더 하길 권한다.
