-- corpus.rules 보호 트리거 두 개를 만든다. 실행 시점: init. CREATE OR REPLACE / DROP IF EXISTS 라 멱등이다.
--   trg_rules_block_truncate  TRUNCATE 를 막는다. 룰 테이블은 사람이 검수한 이력이 누적돼 재생성할 원본이 없다
--   trg_rules_guard           저장 전 검증 트리거: 근거 조문 실재, 표준 비목, 한도 필드 짝, 한도_단위 길이
-- 비목 검사는 corpus.item_vocab 을 조회한다. 여기에 비목 목록을 하드코딩하면 기준 목록이 두 곳이 된다.
-- 신규·변경된 컬럼만 검사한다. 기존 행에 이미 제약을 위반한 값이 있어서, 그 컬럼을 건드리지 않는
-- UPDATE 까지 막히면 아무것도 고칠 수 없게 된다. 각 검사는 IS DISTINCT FROM 으로 변경 여부를 먼저 본다.
-- 정말 재적재가 필요하면 사람이 의도적으로 트리거를 지운다: DROP TRIGGER trg_rules_block_truncate ON corpus.rules;

CREATE OR REPLACE FUNCTION corpus.rules_block_truncate() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  RAISE EXCEPTION
    'corpus.rules TRUNCATE 는 금지되어 있다. 룰 테이블에는 검수 이력이 누적되어 있어 재적재하면 되돌릴 수 없다. '
    '꼭 필요하면 백업 후 DROP TRIGGER trg_rules_block_truncate ON corpus.rules; 를 실행한다.';
END $fn$;

CREATE OR REPLACE FUNCTION corpus.rules_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    g jsonb;
    doc text;
    조번호값 text;
    found boolean;
    한도유형목록 text[] := ARRAY['금액','비율','개수'];
    변경됨 boolean;
BEGIN
    -- 근거: 비어 있으면 안 되고, 배열의 각 {doc_id,조번호} 가 실재하는 조여야 한다
    변경됨 := (TG_OP = 'INSERT') OR (NEW.근거 IS DISTINCT FROM OLD.근거);
    IF 변경됨 THEN
        IF NEW.근거 IS NULL OR jsonb_array_length(NEW.근거) = 0 THEN
            RAISE EXCEPTION 'rules_guard: rule_id=% 근거가 비어 있다. [{doc_id,조번호}] 가 1건 이상 필요하다', NEW.rule_id;
        END IF;
        FOR g IN SELECT jsonb_array_elements(NEW.근거) LOOP
            doc      := g->>'doc_id';
            조번호값 := g->>'조번호';
            IF doc IS NULL OR 조번호값 IS NULL THEN
                RAISE EXCEPTION 'rules_guard: rule_id=% 근거 원소에 doc_id/조번호 가 없다: %', NEW.rule_id, g;
            END IF;
            SELECT EXISTS(
                SELECT 1 FROM corpus.doc_articles a
                WHERE a.doc_id = doc AND a.조번호 = 조번호값 AND NOT a.삭제
            ) INTO found;
            IF NOT found THEN
                RAISE EXCEPTION 'rules_guard: rule_id=% 근거 조가 doc_articles 에 없거나 삭제되었다: % %',
                    NEW.rule_id, doc, 조번호값;
            END IF;
        END LOOP;
    END IF;

    -- 비목: corpus.item_vocab 에 없으면 거부한다
    변경됨 := (TG_OP = 'INSERT') OR (NEW.비목 IS DISTINCT FROM OLD.비목);
    IF 변경됨 AND NOT EXISTS (SELECT 1 FROM corpus.item_vocab v WHERE v.비목 = NEW.비목) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 비목 ''%'' 이 corpus.item_vocab 에 없다',
            NEW.rule_id, NEW.비목;
    END IF;

    -- 한도_유형/한도_값: 같이 있거나 같이 없어야 한다
    변경됨 := (TG_OP = 'INSERT')
              OR (NEW.한도_유형 IS DISTINCT FROM OLD.한도_유형)
              OR (NEW.한도_값 IS DISTINCT FROM OLD.한도_값);
    IF 변경됨 AND ((NEW.한도_유형 IS NULL) <> (NEW.한도_값 IS NULL)) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 한도_유형과 한도_값은 같이 넣거나 같이 비워야 한다', NEW.rule_id;
    END IF;

    IF 변경됨 AND NEW.한도_유형 IS NOT NULL AND NOT (NEW.한도_유형 = ANY(한도유형목록)) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 한도_유형 ''%'' 은 허용되지 않는다(금액/비율/개수 만 허용)', NEW.rule_id, NEW.한도_유형;
    END IF;

    -- 한도_단위: 이번에 변경된 경우만 12자 제한. 기존 위반 행은 이 컬럼을 건드리지 않으면 통과한다
    변경됨 := (TG_OP = 'INSERT') OR (NEW.한도_단위 IS DISTINCT FROM OLD.한도_단위);
    IF 변경됨 AND NEW.한도_단위 IS NOT NULL AND length(NEW.한도_단위) > 12 THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 한도_단위가 %자다. 상한은 12자이고 문장은 사전승인_조건 에 넣는다: %',
            NEW.rule_id, length(NEW.한도_단위), left(NEW.한도_단위, 40);
    END IF;

    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS trg_rules_block_truncate ON corpus.rules;
CREATE TRIGGER trg_rules_block_truncate BEFORE TRUNCATE ON corpus.rules
    FOR EACH STATEMENT EXECUTE FUNCTION corpus.rules_block_truncate();

DROP TRIGGER IF EXISTS "trg_rules_guard" ON corpus.rules;
CREATE TRIGGER trg_rules_guard BEFORE INSERT OR UPDATE ON corpus.rules
    FOR EACH ROW EXECUTE FUNCTION corpus.rules_guard();
