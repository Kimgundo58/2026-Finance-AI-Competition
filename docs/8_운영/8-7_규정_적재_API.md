# 8-7 규정 적재 API (L1·L2)

운영자가 규정 파일을 올리면 서버가 파싱·임베딩·색인까지 마친 뒤 `staged` 로 저장한다. 검토하고 활성화해야
검색에 들어간다. 코드: `server/routes_ingest.py`(HTTP), `scripts/corpus_ingest.py`(파이프라인).
스키마·권한: `db/migrations/2026-11-01_corpus_ingest.sql`(운영), `db/init/18_corpus_ingest.sql`(새 로컬 DB).

## 흐름

```
POST /admin/documents        202, ingest_jobs.상태=parsing
  백그라운드: 추출 -> 조문 분할·품질 판정 -> 조별 적용대상 -> 청크 -> 임베딩(KURE-v1) -> 참조 -> 저장(staged)
  실패: 상태 failed + 실패사유. documents·chunks 에는 아무것도 남지 않는다
GET  /admin/documents/{doc_id}           상태·품질·조 수·경고·조문 샘플
POST /admin/documents/{doc_id}/activate  staged -> active, 이전 판 -> superseded, BM25 색인 (한 트랜잭션)
DELETE /admin/documents/{doc_id}         staged·failed·parsing 폐기
GET  /admin/documents?status=staged      목록
```

상태: `parsing` -> `staged` 또는 `failed` -> `active` -> 다음 판이 활성화되면 `superseded`.
모든 경로는 `X-Admin-Token`(SUDDOE_ADMIN_TOKEN) 이 필요하다. `SUDDOE_INGEST_DSN` 이 없으면 전부 503 이다.

## 운영 적용 (한 번)

1. 마이그레이션 (NCP VM, `deploy/ncp` 에서). 비밀번호는 DSN URL 에 들어가므로 hex 로 만든다.
   ```bash
   PW=$(openssl rand -hex 24)
   docker compose exec -T -e PGCLIENTENCODING=UTF8 db psql -U postgres -d suddoe -X -v ON_ERROR_STOP=1 \
       -v ingest_password="$PW" -f - < ../../db/migrations/2026-11-01_corpus_ingest.sql
   ```
2. `deploy/ncp/.env` 에 `SUDDOE_INGEST_DSN=postgresql://suddoe_ingest:$PW@db:5432/suddoe` 를 추가한다.
   `docker-compose.yml` 이 api 에 넘긴다(비어 있으면 적재 API 만 503).
3. `docker compose up -d --build api`
4. 확인: `curl -s https://$SUDDOE_DOMAIN/admin/documents?limit=3 -H "X-Admin-Token: $SUDDOE_ADMIN_TOKEN"` 가 200

롤 `suddoe_ingest` 는 corpus SELECT, 파이프라인이 쓰는 7개 테이블(documents, doc_articles, chunks, chunk_terms,
chunk_len, refs, ingest_jobs)만 쓰기, term_df REFRESH, judge_cache 삭제 권한을 가진다. 앱 롤 `suddoe_app` 은 그대로다.
마이그레이션은 `term_df(term)` 고유 인덱스도 만든다(활성화의 `REFRESH ... CONCURRENTLY` 에 필요).
되돌리기 SQL 은 마이그레이션 파일 상단 주석에 있다.

로컬: 새 볼륨이면 `18_corpus_ingest.sql` 이 자동 적용된다(비밀번호 `devpw`). 기존 볼륨이면 위 마이그레이션을
`-v ingest_password=devpw` 로 실행하고 `.env` 에 `SUDDOE_INGEST_DSN=postgresql://suddoe_ingest:devpw@localhost:5432/suddoe`.

## 사용 예

```bash
H="X-Admin-Token: $SUDDOE_ADMIN_TOKEN"; API=https://$SUDDOE_DOMAIN
curl -s -X POST $API/admin/documents -H "$H" -F "파일=@초기창업패키지_세부관리기준_2026.hwpx" \
     -F layer=L2 -F 발행기관=창업진흥원 -F "문서명=초기창업패키지 세부관리기준(2026년)" \
     -F 사업명=초기창업패키지 -F 시행일=2026-03-01           # -> {"doc_id": "초기창업패키지 세부관리기준(2026년)_20260301"}
curl -s "$API/admin/documents/초기창업패키지 세부관리기준(2026년)_20260301" -H "$H"   # curl 은 공백을 %20 으로
curl -s -X POST "$API/admin/documents/<doc_id>/activate" -H "$H" -H "Content-Type: application/json" \
     -d '{"대체_doc_id": "초기창업패키지 세부관리기준(2025년)"}'
```

| 필드 | 필수 | 내용 |
|---|---|---|
| 파일 | O | PDF·HWP·HWPX·DOCX·법령 XML, 30MB 이하. 형식은 내용으로 판별(확장자 무시) |
| layer | O | `L1`(중기부 법령·고시·지침) 또는 `L2`(창진원 사업별 기준). 파일명으로 정하지 않는다 |
| 발행기관 | O | 기록용 |
| 문서명 | O | doc_id = `문서명_YYYYMMDD`(시행일). 인용 출처로 화면에 그대로 나온다. `/` 금지 |
| 사업명 | L2 만 | `corpus.programs` 의 사업명 또는 별칭. L1 에 넣으면 422 |
| 시행일 | O | `YYYY-MM-DD` |
| version | | 생략하면 시행일(`2026.3.1`) |
| 적용대상 | | `공통`(기본) · `창업기업` · `주관기관`. 조별 태깅을 하지 않는 문서의 기본값. `주관기관` 은 검색에서 빠진다 |

조별 적용대상: L2 전부와 이름에 관리기준·통합관리지침·운영지침이 들어간 L1 은 조마다 정한다(조제목 주어 > 절 표제 >
본문 첫머리 주어). 부칙·별표는 `공통`. 정하지 못한 조는 조문만 저장하고 청크를 만들지 않는다(경고에 건수).

## 활성화 규칙

- 조건: `staged`, `parse_quality='high'`, 조·청크 1건 이상, 임베딩 누락 없음. 아니면 409.
- 이전 판: 이 API 로 올린 active 문서 중 layer·문서키(괄호·공백 제거한 문서명)·사업명이 같은 것을 자동으로 찾는다.
  못 찾으면 409 와 `후보` 목록을 돌려준다. 대체할 문서가 있으면 `대체_doc_id`, 처음 올리는 문서면 `{"신규문서": true}`.
  신규문서인데 같은 사업(L1 은 같은 이름)의 active 문서가 있으면 409 다. 별개 문서면 `"중복_허용": true` 를 함께 준다.
  기존 코퍼스 문서(이 API 이전에 적재된 문서)는 자동 매칭되지 않으므로 처음 한 번은 `대체_doc_id` 를 준다.
- 이전 판은 `superseded`·`index_target=false` 가 되고 청크를 지운다(조문 doc_articles 는 남긴다). 새 판은
  이전 판의 retrieval_scope 를 이어받는다. 새 판 청크를 BM25 색인(chunk_len·chunk_terms)에 넣고, `tenant.judge_cache` 를
  비우고, `corpus.term_df` 를 `REFRESH ... CONCURRENTLY` 한다. staged 동안에는 BM25 점수에 영향을 주지 않는다.
- 들어오는 참조: active 문서에서 이전 판으로 가는 `corpus.refs` 는 활성화 트랜잭션에서 새 판으로 옮긴다. 판이 바뀌는 순간 참조 확장이 끊기지 않게 하기 위해서다.
- 응답 `경고`: 이전 판을 근거로 가진 corpus.rules 수, 새 판에 없는 조로 옮긴 참조, 신규문서일 때 함께
  검색될 비슷한 active 문서. 룰 근거는 자동으로 고치지 않는다(재검수 대상).
- superseded 를 다시 active 로 돌리는 API 는 없다(청크를 지웠다). 필요하면 그 판을 다시 올려 활성화한다.

## 판정 캐시 반영

- DB 판정 캐시(`tenant.judge_cache`): 활성화 트랜잭션에서 전부 지운다.
- 데이터 버전 해시(`server/response_cache.py` `_데이터버전_해시`): active 청크 수가 바뀌므로 `SUDDOE_DATA_VERSION_TTL`(기본 300초) 안에 바뀐다.
- 게스트 메모리 캐시: 항목마다 데이터 버전 해시를 저장해 비교하므로, 위 해시가 바뀌면 적중하지 않는다.
  즉시 반영이 필요하면 `docker compose restart api`.

## 제한·주의

- 텍스트가 200자 미만인 파일(스캔본)과 배포용·암호화 HWP 는 failed 다. L3 업로드와 달리 OCR 을 하지 않는다.
- 품질 low(조 5개 미만, 단락·장 단위 분할, 조번호 역전, 빈 조 과다)는 활성화할 수 없다. 원본을 고쳐 다시 올린다.
- 적재는 한 번에 하나씩 실행된다. 임베딩이 CPU 를 쓰므로 그동안 판정 응답이 느려질 수 있다. 실측(개발 PC): PDF 480KB·48조·54청크 약 75초.
- API 밖 경로: 복원본 · 법령 HTML 문서는 `scripts/corpus_restored_load.py` + `ontology/수집/적재목록.json` 으로 넣는다. 품질 게이트를 거치지 않으므로 절차는 `ontology/수집/P0-6_P0-7_기록.md` 를 따른다.
- 서버가 재시작되면 parsing 중이던 업로드는 parsing 으로 남는다. DELETE 후 다시 올린다.
- 참조: 새 문서에서 나가는 참조는 active 문서만 대상으로 만든다. `지침 제N조` 는 build_refs 에 고정된
  통합관리지침 제14차 doc_id 로 해결되므로, 새 통합관리지침을 올리면 해결 대상을 따로 확인한다.
- 로컬 DB 는 `eval.golden_chunks` 가 이전 판 청크를 참조하면 활성화가 409 다(골든셋 재매칭 필요). 운영 DB 에는 eval 이 없다.
