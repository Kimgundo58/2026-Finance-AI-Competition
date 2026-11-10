-- QA 계정 2건을 tenant.accounts 에 등록한다.
-- 실행 시점: seed(수동). tenant.orgs 에 기관 목록을 적재한 뒤 실행한다. init 에서는 실행하지 않는다.
--    psql "$DSN" --single-transaction -v ON_ERROR_STOP=1 -f db/seed/12_qa_accounts.sql
--
-- 등록하지 않으면 Supabase 토큰은 발급되지만 tenant.계정찾기(email) 가 org 를 찾지 못해 API 가 403 을 반환한다.
-- 로컬과 운영 양쪽에 실행한다.
-- pw_hash 는 비운다. 비밀번호는 Supabase Auth 가 관리하고 이 테이블은 (email → org_id) 만 담는다.
--
-- org 는 경상국립대학교 창업중심대학사업단이다. uuid 는 tenant.orgs 에 있는 값을 그대로 쓴다.
-- 이름으로 서브쿼리하지 않는다. 비슷한 기관명이 있어 LIKE 로는 다른 기관에 연결될 수 있다.
-- 기존 demo@suddoe.local 계정은 지우지 않는다. .local 도메인은 Supabase 가입이 되지 않아 로그인에 쓰일 수 없다.

-- org 가 있는지 먼저 확인한다. 없는데 INSERT 하면 FK 오류 메시지에 제약 이름만 나와 원인을 알기 어렵다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM tenant.orgs
                   WHERE org_id = 'cfeba091-251a-5ae4-8cc9-88c6e6679440') THEN
        RAISE EXCEPTION
            '경상국립대학교 창업중심대학사업단(cfeba091-…)이 tenant.orgs 에 없다 — '
            '이 DB 에는 기관 목록이 적재되지 않았다. 기관 목록을 먼저 적재한다.';
    END IF;
END
$$;

INSERT INTO tenant.accounts (org_id, email, pw_hash)
VALUES
    ('cfeba091-251a-5ae4-8cc9-88c6e6679440', 'prototype@ssudo.kr', NULL),
    ('cfeba091-251a-5ae4-8cc9-88c6e6679440', 'test@ssudo.kr',      NULL)
ON CONFLICT (email) DO UPDATE
    SET org_id = EXCLUDED.org_id;   -- 기관이 바뀌면 갱신한다. 갱신하지 않으면 이전 org 에 연결된 채로
                                    --    다른 기관 데이터에 접근하게 되고, 이는 403 보다 나쁘다

-- 다시 읽어 확인한다. ON CONFLICT DO UPDATE 는 값이 바뀌지 않아도 rowcount 를 반환하므로 rowcount 로는 확인할 수 없다.
DO $$
DECLARE _n int;
BEGIN
    SELECT count(*) INTO _n FROM tenant.accounts
     WHERE email IN ('prototype@ssudo.kr','test@ssudo.kr')
       AND org_id = 'cfeba091-251a-5ae4-8cc9-88c6e6679440';
    IF _n <> 2 THEN
        RAISE EXCEPTION 'QA 계정 2건이 경상국립대에 연결되지 않았다 (실제 %건)', _n;
    END IF;
    RAISE NOTICE 'QA 계정 2건 확인: 경상국립대학교 창업중심대학사업단';
END
$$;
