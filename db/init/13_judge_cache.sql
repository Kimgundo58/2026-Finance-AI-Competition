-- ════════════════════════════════════════════════════════════════════════════
-- 13_judge_cache.sql — 판정 캐시를 DB 로 옮긴다 (Q5, 2026-09-04)
--
-- 지금까지 `server/main.py:111` 의 `비용가드._캐시` 는 프로세스 메모리 dict 였다 —
-- Cloud Run 이 재배포·재기동하면 통째로 날아간다. QA 중 GPU 가 꺼져 있어도
-- 미리 구운 답이 0.25초에 나가게 하려면 캐시가 프로세스보다 오래 살아야 한다.
--
-- 🔴 **키를 굵게 잡지 않는다 — Q4(ai-f2) 실측(2026-09-04)이 이유다.**
--    golden_set 113 중 비목이 채워진 60행을 (사업명,비목) 로 묶으면 충돌 9그룹·31문항,
--    그중 7/9 가 정답 판정이 갈린다(예: 예비창업패키지 인건비 안에 가능/불가 공존,
--    초기창업패키지 인건비는 가능/불가/불가 셋). (품목+비목+사업명) 도 마찬가지로 굵다.
--    → 굵은 키는 「적중률이 낮다」가 아니라 **「캐시가 오답을 확신 있게 낸다」** 다.
--    판단불가는 안전한 실패지만 캐시가 만든 오답은 이 프로젝트에서 제일 나쁜 결과다.
--    그래서 이 테이블의 `key` 는 여전히 `main.py::비용가드.열쇠()` 그대로 쓴다 —
--    org_id + 사업명 + 확정비목 + **정규화 산출 전체(JSON)** + f5 두 필드 + 목,
--    전부를 sha256 한 값이다. 반복 질문만 맞고 히트율은 낮다 — **그게 목적이다.**
--    QA 시나리오가 밟을 질문 N개를 미리 구워 두는 것이 이 캐시의 일이지 범용
--    적중률을 올리는 것이 아니다. 「히트율이 낮으니 키를 굵게 하자」로 되돌리면
--    위 7/9 오답 공존이 그대로 캐시에 확신을 달고 나간다 — 이 주석은 그걸 막는 것이 일이다.
--
-- 🔴 **게스트(org 없음)는 이 캐시를 안 쓴다.** `tenant.*` RLS 정책은 전부
--    `USING (org_id = tenant.current_org())` 다. org_id·current_org() 어느 한쪽이라도
--    NULL 이면 `NULL = NULL` 이 TRUE 가 아니라 NULL 이라 통과하지 못한다
--    (`10_rls_guc.sql` 미결 ②와 같은 자리 — 정책을 고치는 건 오너 결정이라 여기서
--    안 건드린다). 그래서 `org_id` 를 NOT NULL 로 두고, 서버 쪽(`main.py`)도 org 가
--    없는 요청은 캐시 조회·저장 자체를 건너뛴다 — 이 테이블에 NULL 행은 없다.
--
-- 🔴 **`설정_해시` 가 없으면 캐시가 낡은 답을 낸다.** 룰 재검수·코퍼스 재적재가
--    일어나도 캐시는 그걸 모른다 — `scripts/eval_store.코퍼스버전()` 과 같은 발상으로
--    (청크수·임베딩수·refs수·문서수·최대chunk_id·룰수·검수룰수) 를 해시해 조회 시점에
--    현재 값과 다르면 미스로 취급한다(행을 지우진 않는다 — `expires_at` TTL 이 걷는다).
--    조건이 다른 run 을 안 섞는 것과 같은 이유다(CLAUDE.md 「지표를 읽을 때」).
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE tenant.judge_cache (
    key         TEXT PRIMARY KEY,        -- 비용가드.열쇠() 의 sha256 (정규화 산출 전체 포함)
    org_id      UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    종류        TEXT NOT NULL CHECK (종류 IN ('normalize', 'judge')),
    value       JSONB NOT NULL,
    설정_해시   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_judge_cache_expires ON tenant.judge_cache (expires_at);
CREATE INDEX ix_judge_cache_org     ON tenant.judge_cache (org_id, 종류);

ALTER TABLE tenant.judge_cache ENABLE ROW LEVEL SECURITY;
CREATE POLICY org_isolation ON tenant.judge_cache USING (org_id = tenant.current_org());

-- `10_rls_guc.sql`(로컬)·`10b_cloudsql_grant.sql`(운영) 의 ALTER DEFAULT PRIVILEGES 가
-- 이미 걸려 있어 새 테이블도 자동 상속받지만, 이 파일 하나만 따로 재생(replay)하는
-- 경우를 대비해 명시로 한 번 더 준다 — 멱등이라 다시 쳐도 안전하다.
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant.judge_cache TO suddoe_app;

-- ════════════════════════════════════════════════════════════════════════════
-- 검증 (친 뒤 반드시 이걸로 확인 — 중앙 지시, 2026-09-04)
--
--   ① 새 트랜잭션·새 연결로 되읽기:
--        select set_config('app.org_id','<실재 org_id>', true);
--        insert into tenant.judge_cache(key,org_id,종류,value,설정_해시,expires_at)
--          values ('t1','<같은 org_id>','judge','{"x":1}','h1', now()+interval '1 min');
--        -- 새 psql 세션으로 다시 붙어서:
--        select set_config('app.org_id','<같은 org_id>', true);
--        select value from tenant.judge_cache where key='t1';        → {"x":1} 이 나와야 한다
--
--   ② 비특권 롤(suddoe_app)로 위 왕복을 그대로 반복 — postgres(superuser)는
--      bypassrls 라 이 자리가 안 보인다.
--
--   ③ 다른 org 로 GUC 를 세우고 같은 key 를 SELECT → 0행이어야 한다.
--
--   ④ 정리: delete from tenant.judge_cache where key='t1';
-- ════════════════════════════════════════════════════════════════════════════
