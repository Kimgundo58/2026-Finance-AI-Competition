-- 필수. 판정 결과의 「맞았어요 / 틀렸어요」 저장 표와 관리 조회 함수를 더한다. 판정 코드는 이 표를 읽지 않는다.
-- 코드 배포 전에 적용한다(새 코드의 계획 상세가 이 표를 읽는다). 이전 코드는 읽지 않으므로 api 를 멈추지 않는다.
-- 실행: docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < ../../db/migrations/2026-11-06_판정_피드백.sql
BEGIN;
CREATE TABLE IF NOT EXISTS tenant.decision_feedback (
    feedback_id      BIGSERIAL PRIMARY KEY,
    decision_id      BIGINT NOT NULL REFERENCES tenant.decisions(decision_id) ON DELETE CASCADE,
    org_id           UUID NOT NULL REFERENCES tenant.orgs(org_id) ON DELETE CASCADE,
    -- 판정의 소유 계정(RLS 기준). 데모 세션은 NULL 이다
    account_id       UUID REFERENCES tenant.accounts(account_id) ON DELETE CASCADE,
    -- 평가를 남긴 계정
    작성_account_id  UUID REFERENCES tenant.accounts(account_id) ON DELETE CASCADE,
    평가             TEXT NOT NULL CHECK (평가 IN ('맞음', '틀림')),
    이유             TEXT CHECK (이유 IN ('판정이 다름', '근거가 다름', '기타')),
    의견             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (decision_id, 작성_account_id),
    CONSTRAINT decision_feedback_평가이유_check CHECK ((평가 = '틀림') = (이유 IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_decision_feedback_틀림
    ON tenant.decision_feedback (updated_at DESC) WHERE 평가 = '틀림';

ALTER TABLE tenant.decision_feedback ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS account_isolation ON tenant.decision_feedback;
CREATE POLICY account_isolation ON tenant.decision_feedback USING (
    org_id = tenant.current_org() AND account_id IS NOT DISTINCT FROM tenant.current_account());
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant.decision_feedback TO suddoe_app;
GRANT USAGE, SELECT ON SEQUENCE tenant.decision_feedback_feedback_id_seq TO suddoe_app;

-- 관리 화면 조회. RLS 는 기관을 넘어 읽지 못하게 막으므로 「틀렸어요」 목록만 여는 함수를 따로 둔다.
-- 이메일과 계정 ID 는 돌려주지 않는다. 호출 경로는 관리자 토큰을 검사하는 GET /admin/feedback 하나다.
CREATE OR REPLACE FUNCTION tenant.틀림_피드백목록(_limit integer, _offset integer)
RETURNS TABLE (decision_id bigint, plan_id bigint, 기관명 text, 이유 text, 의견 text,
               질문원문 text, 판정 text, 요약 text, 저장시각 timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, tenant
AS $$
    SELECT f.decision_id, d.plan_id::bigint, o.기관명, f.이유, f.의견,
           d.질문원문, d.판정::text, d.요약, f.updated_at
      FROM tenant.decision_feedback f
      JOIN tenant.decisions d ON d.decision_id = f.decision_id
      JOIN tenant.orgs o ON o.org_id = f.org_id
     WHERE f.평가 = '틀림'
     ORDER BY f.updated_at DESC
     LIMIT least(greatest(_limit, 0), 200) OFFSET greatest(_offset, 0)
$$;
REVOKE ALL ON FUNCTION tenant.틀림_피드백목록(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tenant.틀림_피드백목록(integer, integer) TO suddoe_app;
COMMIT;
