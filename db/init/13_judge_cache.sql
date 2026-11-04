-- 판정·정규화 응답 캐시 tenant.judge_cache. 실행 시점: init. 멱등이다.
-- 프로세스 메모리 캐시는 재시작하면 사라지므로 기관(org) 요청의 캐시는 DB 에 둔다.
--
-- 캐시 키를 넓게 잡지 않는다. (사업명, 비목) 이나 (품목, 비목, 사업명) 으로 묶으면 같은 키 아래
-- 정답 판정이 다른 문항이 섞인다. 범위가 넓은 키는 적중률보다 캐시가 오답을 그대로 반환하는 것이 문제다.
-- 판단불가는 안전한 실패지만 캐시가 만든 오답은 가장 나쁜 결과다.
-- key 는 server/response_cache.캐시키() 가 만든 sha256 이다. org_id, account_id, 사업명, 확정비목, 정규화 결과 전체(JSON),
-- 사용자 입력 필드, 기관 L3 문서 해시(response_cache.l3_해시, 판정만)이 해시 입력에 들어간다.
-- 같은 질문이 반복될 때만 적중하는 것이 의도다.
--
-- 게스트(org 없음)는 이 캐시를 쓰지 않는다(서버 프로세스 메모리 캐시를 쓴다). RLS 정책이
-- org_id = current_org() 라 한쪽이 NULL 이면 비교 결과가 NULL 이 되어 통과하지 못한다.
--
-- 데이터버전_해시 는 룰 재검수·코퍼스 재적재를 캐시가 알아채기 위한 값이다(server/response_cache.py 가 계산한다).
-- 코퍼스·룰·우선순위·체크항목·별칭 상태를 해시한 값이 조회 시점 값과 다르면 미스로 처리한다. 행은 지우지 않고 expires_at 이 지나면 무시된다.
-- 규정 적재 API 의 활성화는 이 테이블을 전부 지운다(18_corpus_ingest.sql 의 ingest_cache_purge).

CREATE TABLE IF NOT EXISTS tenant.judge_cache (
    key         TEXT PRIMARY KEY,        -- 캐시 키(sha256)
    org_id      UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    -- NULL = 계정 없는 기관(데모 세션). 판정 응답은 계정의 계획·프로필에 묶이므로 계정마다 따로 둔다
    account_id  UUID REFERENCES tenant.accounts(account_id) ON DELETE CASCADE,
    종류        TEXT NOT NULL CHECK (종류 IN ('normalize', 'judge')),
    value       JSONB NOT NULL,
    데이터버전_해시 TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_judge_cache_expires ON tenant.judge_cache (expires_at);
CREATE INDEX IF NOT EXISTS ix_judge_cache_org     ON tenant.judge_cache (org_id, 종류);

ALTER TABLE tenant.judge_cache ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_isolation ON tenant.judge_cache;
CREATE POLICY account_isolation ON tenant.judge_cache USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());

-- 10b_app_grants.sql 의 ALTER DEFAULT PRIVILEGES 로 새 테이블도 권한을 받지만,
-- 이 파일만 따로 실행하는 경우를 위해 명시적으로 한 번 더 부여한다(멱등).
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant.judge_cache TO suddoe_app;

-- 검증 (적용 후 suddoe_app 으로 확인한다. postgres 는 RLS 를 우회해 차이가 보이지 않는다)
--   1. 쓰기 후 새 커넥션에서 읽기:
--        select set_config('app.org_id','<실재 org_id>', true);
--        insert into tenant.judge_cache(key,org_id,종류,value,데이터버전_해시,expires_at)
--          values ('t1','<같은 org_id>','judge','{"x":1}','h1', now()+interval '1 min');
--        -- 새 psql 세션에서:
--        select set_config('app.org_id','<같은 org_id>', true);
--        select value from tenant.judge_cache where key='t1';        -> {"x":1}
--   2. 다른 org 로 GUC 를 설정하고 같은 key 를 SELECT -> 0행
--   3. 정리: delete from tenant.judge_cache where key='t1';
