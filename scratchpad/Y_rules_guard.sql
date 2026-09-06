-- corpus.rules 저장 전 구조·참조 무결성 게이트 (레인 Y 제안 — DDL 미적용, 중앙이 검토 후 적용)
-- 🔴 scripts/archive/ 는 이 세션에서 쓰기가 막혀 있어(읽기전용 가드) scratchpad 에 낸다.
--    최종 위치는 중앙이 정한다 — 아마 db/init/ 아래(스키마 파일) 가 맞을 것이다.
--
-- 자리 결정 근거 (scratchpad/보고 참고):
--   ① 원문 문자대조("원문발췌가 그 조 본문에 있는가")는 corpus.rules 에 원문발췌를
--      담을 칸 자체가 없어 DB 트리거로 «원천적으로» 못 한다 → 이건 DB 밖(제안 JSON
--      단계, scratchpad/룰검산.py류)에서 의무 게이트로 잡는다. 이 파일의 범위 밖이다.
--   ② 구조·참조 무결성(근거 조 실재·비목 enum·한도 필드 짝·한도_단위 길이)은 트리거로
--      건다 — corpus.rules 쓰기 경로가 3개(적재스크립트 seed_rules.py · 검수툴
--      review_rules.py 의 UPDATE · 수기 SQL)인데, 트리거만이 «어느 경로든» 걸린다.
--
-- 🔴 신규·수정분만 걸리게 짰다. 기존 74행 «소급 검사» 는 안 한다:
--    - 한도_단위 12자 제한은 살아있는 74행 중 13행이 «이미» 위반이다(실측, 2026-09-06).
--      이 13행을 건드리지 «않는» UPDATE(예: 사전승인_조건만 고치는 것)까지 막히면
--      "아무것도 못 고친다"가 실제로 벌어진다. 그래서 한도_단위 칸 자체가 «이번
--      UPDATE 로 바뀔 때만» 길이를 본다(IS DISTINCT FROM 가드). INSERT 는 항상 본다
--      (신규는 처음부터 맞게 넣으라는 뜻).
--    - 나머지 검사(근거·비목·한도짝)는 실측 결과 기존 74행이 전부 통과라 가드가
--      필요 없었다 — 그래도 혹시 모를 레거시 예외를 위해 같은 원칙(바뀐 칼럼만 본다)을
--      전부에 동일하게 적용해뒀다.

CREATE OR REPLACE FUNCTION corpus.rules_guard() RETURNS trigger AS $$
DECLARE
    g jsonb;
    doc text;
    jo text;
    found boolean;
    비목목록 text[] := ARRAY['재료비','외주용역비','기계장치','특허권등무형자산취득비',
                             '인건비','지급수수료','여비','교육훈련비','광고선전비','창업활동비'];
    한도유형목록 text[] := ARRAY['금액','비율','개수'];
    바뀐 boolean;
BEGIN
    -- ── 근거: 비어있으면 안 되고, 배열의 각 {doc_id,조번호} 가 실재 조여야 한다 ──
    바뀐 := (TG_OP = 'INSERT') OR (NEW.근거 IS DISTINCT FROM OLD.근거);
    IF 바뀐 THEN
        IF NEW.근거 IS NULL OR jsonb_array_length(NEW.근거) = 0 THEN
            RAISE EXCEPTION 'rules_guard: rule_id=% 근거가 비었다 — [{doc_id,조번호}] 최소 1건 필수', NEW.rule_id;
        END IF;
        FOR g IN SELECT jsonb_array_elements(NEW.근거) LOOP
            doc := g->>'doc_id';
            jo  := g->>'조번호';
            IF doc IS NULL OR jo IS NULL THEN
                RAISE EXCEPTION 'rules_guard: rule_id=% 근거 원소에 doc_id/조번호 가 없다: %', NEW.rule_id, g;
            END IF;
            SELECT EXISTS(
                SELECT 1 FROM corpus.doc_articles a
                WHERE a.doc_id = doc AND a.조번호 = jo AND NOT a.삭제
            ) INTO found;
            IF NOT found THEN
                RAISE EXCEPTION 'rules_guard: rule_id=% 근거 조가 doc_articles 에 없다(또는 삭제됨): % %',
                    NEW.rule_id, doc, jo;
            END IF;
        END LOOP;
    END IF;

    -- ── 비목: 정본 10종 밖이면 거부 ──
    바뀐 := (TG_OP = 'INSERT') OR (NEW.비목 IS DISTINCT FROM OLD.비목);
    IF 바뀐 AND NOT (NEW.비목 = ANY(비목목록)) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 비목 ''%'' 이 정본 10종이 아니다', NEW.rule_id, NEW.비목;
    END IF;

    -- ── 한도_유형/한도_값: 같이 있거나 같이 없어야 한다 ──
    바뀐 := (TG_OP = 'INSERT')
            OR (NEW.한도_유형 IS DISTINCT FROM OLD.한도_유형)
            OR (NEW.한도_값 IS DISTINCT FROM OLD.한도_값);
    IF 바뀐 AND ((NEW.한도_유형 IS NULL) <> (NEW.한도_값 IS NULL)) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 한도_유형·한도_값 은 같이 넣거나 같이 비워야 한다', NEW.rule_id;
    END IF;

    IF 바뀐 AND NEW.한도_유형 IS NOT NULL AND NOT (NEW.한도_유형 = ANY(한도유형목록)) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 한도_유형 ''%'' 이 이상하다(금액/비율/개수 만)', NEW.rule_id, NEW.한도_유형;
    END IF;

    -- ── 한도_단위: «이번에 바뀐 경우만» 12자 제한. 기존 위반행은 안 건드리면 통과 ──
    바뀐 := (TG_OP = 'INSERT') OR (NEW.한도_단위 IS DISTINCT FROM OLD.한도_단위);
    IF 바뀐 AND NEW.한도_단위 IS NOT NULL AND length(NEW.한도_단위) > 12 THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 한도_단위가 %자 — 상한 12자. 문장은 사전승인_조건 으로 보내라: %',
            NEW.rule_id, length(NEW.한도_단위), left(NEW.한도_단위, 40);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- DROP TRIGGER IF EXISTS trg_rules_guard ON corpus.rules;
-- CREATE TRIGGER trg_rules_guard
--     BEFORE INSERT OR UPDATE ON corpus.rules
--     FOR EACH ROW EXECUTE FUNCTION corpus.rules_guard();

-- ════════════════════════════════════════════════════════════════════════
-- 실측 (2026-09-06, 이 파일의 로직을 Python 으로 그대로 재현해 74행 전체에 시뮬레이션)
--   근거 실재·비목 enum·한도 필드짝: 위반 0행
--   한도_단위 12자 초과(=한도_단위 칼럼을 만졌다면 걸렸을 행): 13행
--     428·431·438·441·447·456·459·468·474·476·477·485·418
--     -> scratchpad/룰제안_룰E.json 이 이미 이 13행을 고치는 제안이다(별도 레인, 아직 미적용).
--        이 트리거는 그 13행을 "건드리지 않는 한" 통과시킨다 — 소급 검사 아님.
-- ════════════════════════════════════════════════════════════════════════
