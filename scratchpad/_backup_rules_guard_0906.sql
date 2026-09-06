-- 2026-09-06 변경 «전» 원본 (되돌릴 때 이 본문으로 CREATE OR REPLACE)
CREATE OR REPLACE FUNCTION rules_guard() RETURNS trigger LANGUAGE plpgsql AS $backup$

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

$backup$;
