-- QA 팀원 계정 2건을 명부에 올린다.
-- 이 파일이 없으면 로그인은 되는데 API 가 전부 403 이다 — 토큰은 정상 발급되지만
-- tenant.계정찾기(email) 로 org 를 못 찾으면 「등록되지 않은 계정」이 된다. 처방은 INSERT 두 줄이다.
--
-- 로컬과 운영 양쪽에 넣는다 — 로컬만 넣으면 운영은 그대로 403 이다.
-- 운영 Cloud SQL 은 인증된 네트워크가 아니면 타임아웃이다 — authorized-networks 등록이 선행이다.
--
-- pw_hash 는 비운다 — 비밀번호는 Supabase 가 들고, 우리는 (email → org_id) 만 든다.
--
-- org 는 경상국립대학교 창업중심대학사업단이다. uuid 는 tenant.orgs 에 실재하는 값을 그대로 박는다 —
-- 이름으로 서브쿼리하지 않는다. 유사 기관명이 같이 있어 LIKE 한 글자 차이로 다른 기관에 붙을 수 있다.
--
-- 기존 demo@suddoe.local 계정은 지우지 않는다 — .local 도메인이라 Supabase 가입이 안 돼 데모 경로로 흘러들 수 없다.
--
--    psql "$DSN" --single-transaction -v ON_ERROR_STOP=1 -f db/init/12_qa_accounts.sql

-- org 가 실재하는지 먼저 본다 — 없는데 INSERT 하면 FK 에러 메시지가 제약 이름만 나와 원인이 안 보인다.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM tenant.orgs
                   WHERE org_id = 'cfeba091-251a-5ae4-8cc9-88c6e6679440') THEN
        RAISE EXCEPTION
            '경상국립대학교 창업중심대학사업단(cfeba091-…)이 tenant.orgs 에 없다 — '
            '이 DB 는 기관 명부가 안 실린 인스턴스다. 명부부터 적재해라.';
    END IF;
END
$$;

INSERT INTO tenant.accounts (org_id, email, pw_hash)
VALUES
    ('cfeba091-251a-5ae4-8cc9-88c6e6679440', 'prototype@ssudo.kr', NULL),
    ('cfeba091-251a-5ae4-8cc9-88c6e6679440', 'test@ssudo.kr',      NULL)
ON CONFLICT (email) DO UPDATE
    SET org_id = EXCLUDED.org_id;   -- 기관이 바뀌면 갱신한다 — 조용히 넘기면 낡은 org 에 붙은 채로
                                    --    남의 기관 데이터가 열린다. 그게 403 보다 나쁘다

-- 되읽기 검증 — rowcount 로 통과시키지 않는다. ON CONFLICT DO UPDATE 는 안 바뀌어도 rowcount 를 주므로 실제로 다시 읽어 본다.
DO $$
DECLARE _n int;
BEGIN
    SELECT count(*) INTO _n FROM tenant.accounts
     WHERE email IN ('prototype@ssudo.kr','test@ssudo.kr')
       AND org_id = 'cfeba091-251a-5ae4-8cc9-88c6e6679440';
    IF _n <> 2 THEN
        RAISE EXCEPTION 'QA 계정 2건이 경상국립대에 붙지 않았다 (실제 %건)', _n;
    END IF;
    RAISE NOTICE 'QA 계정 2건 확인 — 경상국립대학교 창업중심대학사업단';
END
$$;
