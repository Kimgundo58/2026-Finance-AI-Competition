-- 판정 캐시를 DB 로 옮긴다. 프로세스 메모리 dict 는 Cloud Run 재배포·재기동에 통째로 날아간다 —
-- 캐시가 프로세스보다 오래 살아야 GPU 가 꺼져 있어도 미리 구운 답을 낼 수 있다.
--
-- 키를 굵게 잡지 않는다 — (사업명,비목) 나 (품목+비목+사업명) 처럼 묶으면 같은 키 아래 정답 판정이
-- 갈리는 문항들이 섞인다. 굵은 키는 적중률이 낮은 게 문제가 아니라 캐시가 오답을 확신 있게 낸다는
-- 게 문제다 — 판단불가는 안전한 실패지만 캐시가 만든 오답이 제일 나쁘다. key 는 main.py::비용가드.열쇠()
-- 그대로 쓴다 — org_id + 사업명 + 확정비목 + 정규화 산출 전체(JSON) + f5 두 필드 + 목, 전부를 sha256 한 값이다.
-- 반복 질문만 맞고 히트율은 낮다 — 그게 목적이다. 범용 적중률을 올리는 캐시가 아니다.
--
-- 게스트(org 없음)는 이 캐시를 안 쓴다 — RLS 정책은 org_id = current_org() 인데 어느 한쪽이라도
-- NULL 이면 NULL=NULL 이 TRUE 가 아니라 NULL 이라 통과 못 한다. org_id 를 NOT NULL 로 두고
-- 서버도 org 없는 요청은 캐시 조회·저장을 건너뛴다.
--
-- 설정_해시 가 없으면 캐시가 낡은 답을 낸다 — 룰 재검수·코퍼스 재적재를 캐시는 모른다.
-- (청크수·임베딩수·refs수·문서수·최대chunk_id·룰수·검수룰수) 를 해시해 조회 시점 값과 다르면 미스로 취급한다
-- (행을 지우진 않는다 — expires_at TTL 이 걷는다).

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

-- 검증 (친 뒤 반드시 이걸로 확인)
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
