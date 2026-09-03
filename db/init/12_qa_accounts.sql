-- ════════════════════════════════════════════════════════════════════════════
-- 12_qa_accounts.sql — QA 팀원 계정 2건을 명부에 올린다.   2026-09-04
--
-- 🔴 **이 파일이 없으면 로그인은 되는데 API 가 전부 403 이다.**
--    Supabase 로그인은 성공해서 토큰이 정상으로 나온다(ES256 · aud=authenticated).
--    그런데 `auth._계정조회()` 가 `tenant.계정찾기(email)` 로 org 를 찾는데
--    명부에 그 이메일이 없다 → `403 등록되지 않은 계정이다`.
--    실측(2026-09-04 · 실서버 · prototype 토큰 지참):
--        /api/health  200          ← 서버도 DB 도 살아 있다
--        /api/profile 403 · /api/plans 403 · /api/tasks 403
--    「DB 가 막혔다」가 아니라 **신원이 없다**. 처방은 INSERT 두 줄이고 코드는 안 바뀐다.
--
-- 🔴 **로컬과 운영 «양쪽» 에 넣는다.** 로컬만 넣으면 QA 당일 그대로 403 이다.
--    운영 Cloud SQL 은 인증된 네트워크가 아니면 타임아웃이다 —
--    `gcloud sql instances patch suddoe-db --authorized-networks=<내IP>/32` 가 선행이다.
--
-- 🔴 `pw_hash` 는 비운다. 비밀번호는 Supabase 가 든다 — 우리가 드는 것은
--    (email → org_id) 뿐이다. `server/auth.py` 모듈 docstring 이 그 근거다.
--    여기에 해시를 넣으면 「우리도 비밀번호를 든다」가 되어 로테이션 대상이 둘로 늘어난다.
--
-- 🔴 org 는 **경상국립대학교 창업중심대학사업단** 이다 (0904 오너 확정 · 건국대는 드랍).
--    uuid 는 `tenant.orgs` 에 실재하는 값을 그대로 박는다 — 새로 만들지 않는다.
--    이름으로 서브쿼리하지 않는 이유: 「경상국립대학교」·「경상국립대학교 창업지원단」이
--    같이 있어서 LIKE 한 글자 차이로 **다른 기관에 붙는다**. 세 행 중 하나를 골라야 한다.
--
-- ⚠️ 기존 `demo@suddoe.local`(→ 건국대학교 86cbda02-…) 은 **지우지 않는다.**
--    `.local` 도메인이라 Supabase 가입이 안 되고, 따라서 토큰이 안 난다 —
--    데모 경로로 흘러들 수 없다. 기록으로 남긴다.
--
--    psql "$DSN" --single-transaction -v ON_ERROR_STOP=1 -f db/init/12_qa_accounts.sql
-- ════════════════════════════════════════════════════════════════════════════

-- 🔴 org 가 실재하는지 «먼저» 본다. 없는데 INSERT 하면 FK 로 터지는데,
--    그 에러 메시지는 「기관이 없다」가 아니라 제약 이름이라 원인이 안 보인다.
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
    SET org_id = EXCLUDED.org_id;   -- 🔴 기관이 바뀌면 «갱신» 이다. 조용히 넘기면
                                    --    낡은 org 에 붙은 채로 403 이 아니라 «남의 기관»
                                    --    데이터가 열린다. 그게 403 보다 나쁘다

-- ── 되읽기 검증 — 🔴 rowcount 로 통과시키지 않는다 ──────────────────────────
-- `ON CONFLICT DO UPDATE` 는 아무것도 안 바뀌어도 rowcount 를 준다.
-- 실제로 «지금 테이블에 무엇이 있나» 를 다시 읽어 본다.
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
