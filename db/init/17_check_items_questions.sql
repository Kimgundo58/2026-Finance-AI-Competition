-- 서버 코드가 읽지만 앞선 init 파일에 없던 컬럼을 추가하고, 적재 현황 뷰 corpus.v_적재현황 을 만든다.
-- 실행 시점: init (빈 볼륨에서 컨테이너를 처음 만들 때). 운영 DB 와 기존 로컬 DB 에는 컬럼이 이미 있으므로
-- 마이그레이션은 필요 없다. 전부 IF NOT EXISTS / OR REPLACE 라 다시 실행해도 같은 상태가 된다.
-- 이 컬럼이 없으면 심층질문이 빈 목록이 되고 해야할일 기한의 근거가 빠진다.

-- corpus.check_items: 심층질문(체크항목을 사용자에게 묻는 질문)과 기한 근거
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "질문문"        TEXT;
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "선택지"        JSONB;
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "질문유형"      TEXT;
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "필요프로필필드" TEXT[];
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "기한근거"      TEXT;
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "기한근거_상세" TEXT;

-- eval.golden_set: 평가 문항 분류. 앱은 읽지 않고 평가 스크립트만 읽는다.
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS "대상"     TEXT;
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS "평가범위" TEXT;
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS "채점모드" TEXT;

COMMENT ON COLUMN eval.golden_set."채점모드" IS
    '전체 | 가능오판만 | 판정만. 문항을 만들 때 기록한 채점 범위 표시다. 평가 코드는 이 값으로 분기하지 않는다.';
COMMENT ON COLUMN eval.golden_set."평가범위" IS
    '핵심 | 유효 | 범위밖(사유). 주관기관 대상 문항은 검색 필터(적용대상 IN (창업기업, 공통)) 때문에 '
    '구조적으로 검색되지 않으므로 범위밖으로 표시한다.';


-- 적재 현황 확인용 뷰. 모든 테이블이 생긴 뒤 만들어야 하므로 마지막 init 파일에 둔다.
-- 로컬 개발 DB 용이다(eval 을 참조하므로 eval 이 없는 운영 DB 에는 만들지 않는다).
-- pgweb(http://localhost:8081) 에서 이 뷰를 먼저 확인한다. 테이블을 추가하면 여기에도 추가한다.
CREATE OR REPLACE VIEW corpus.v_적재현황 AS
SELECT '01. corpus.documents'      AS 테이블, count(*) AS 건수 FROM corpus.documents
UNION ALL SELECT '02. corpus.doc_articles',     count(*) FROM corpus.doc_articles
UNION ALL SELECT '03. corpus.chunks',           count(*) FROM corpus.chunks
UNION ALL SELECT '04. corpus.chunk_terms',      count(*) FROM corpus.chunk_terms
UNION ALL SELECT '05. corpus.chunk_len',        count(*) FROM corpus.chunk_len
UNION ALL SELECT '06. corpus.rules',            count(*) FROM corpus.rules
UNION ALL SELECT '07. corpus.precedence_rules', count(*) FROM corpus.precedence_rules
UNION ALL SELECT '08. corpus.refs',             count(*) FROM corpus.refs
UNION ALL SELECT '09. corpus.item_alias',       count(*) FROM corpus.item_alias
UNION ALL SELECT '10. corpus.item_vocab',       count(*) FROM corpus.item_vocab
UNION ALL SELECT '11. corpus.programs',         count(*) FROM corpus.programs
UNION ALL SELECT '12. corpus.check_items',      count(*) FROM corpus.check_items
UNION ALL SELECT '13. corpus.evidence_sources', count(*) FROM corpus.evidence_sources
UNION ALL SELECT '14. corpus.xref_mismatch',    count(*) FROM corpus.xref_mismatch
UNION ALL SELECT '15. corpus.recheck_queue',    count(*) FROM corpus.recheck_queue
UNION ALL SELECT '16. tenant.orgs',             count(*) FROM tenant.orgs
UNION ALL SELECT '17. tenant.org_programs',     count(*) FROM tenant.org_programs
UNION ALL SELECT '18. tenant.accounts',         count(*) FROM tenant.accounts
UNION ALL SELECT '19. tenant.l3_documents',     count(*) FROM tenant.l3_documents
UNION ALL SELECT '20. tenant.l3_articles',      count(*) FROM tenant.l3_articles
UNION ALL SELECT '21. tenant.profile_agreements',   count(*) FROM tenant.profile_agreements
UNION ALL SELECT '22. tenant.profile_expenditures', count(*) FROM tenant.profile_expenditures
UNION ALL SELECT '23. tenant.profile_personnel',    count(*) FROM tenant.profile_personnel
UNION ALL SELECT '24. tenant.expense_plans',    count(*) FROM tenant.expense_plans
UNION ALL SELECT '25. tenant.plan_tasks',       count(*) FROM tenant.plan_tasks
UNION ALL SELECT '26. tenant.decisions',        count(*) FROM tenant.decisions
UNION ALL SELECT '27. tenant.judge_cache',      count(*) FROM tenant.judge_cache
UNION ALL SELECT '28. tenant.incidents',        count(*) FROM tenant.incidents
UNION ALL SELECT '29. tenant.unmapped_premise', count(*) FROM tenant.unmapped_premise
UNION ALL SELECT '30. eval.golden_set',         count(*) FROM eval.golden_set
UNION ALL SELECT '31. eval.golden_chunks',      count(*) FROM eval.golden_chunks
UNION ALL SELECT '32. eval.runs',               count(*) FROM eval.runs
UNION ALL SELECT '33. eval.run_items',          count(*) FROM eval.run_items
ORDER BY 1;
