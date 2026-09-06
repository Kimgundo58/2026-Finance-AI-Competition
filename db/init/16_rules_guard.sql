-- ════════════════════════════════════════════════════════════════════
-- corpus.rules 저장 전 «문지기» — 근거 실재 · 비목 정본 · 한도 필드 짝 · 한도_단위 길이
-- ════════════════════════════════════════════════════════════════════
-- 🔴 2026-09-06 — 이 파일이 «없었다». 함수는 운영 DB 에만 살아 있고 레포엔 scratchpad
--    (`Y_rules_guard.sql`, 레인 Y 제안본)뿐이었다. DB 를 새로 초기화하면 검사가
--    «통째로 사라진다». 이 파일은 «지금 도는 것을 그대로» 박은 것이다.
--
-- 🔴 비목 검사는 하드코딩 10종에서 `corpus.item_vocab` 조회로 바뀌었다 (2026-09-06).
--    10종은 창업 계통뿐이라 TIPS(RND 8종)가 8행 중 7행 거부됐다. 명단을 여기서 손으로
--    늘리면 «정본이 두 곳» 이 된다 — item_vocab 은 이미 PK 가 (비목,계통) 으로
--    넓혀졌는데(5ca89ad) 문지기만 확장 «전» 이었다. 출처를 하나로 모은다.
--    ✅ 검사를 «없앤» 게 아니다: 적용 직후 대조군으로 확인했다 —
--       '없는비목XYZ' INSERT → 거부 · '연구활동비' INSERT → 통과.
--
-- 🔴 신규·수정분만 건다. 기존 행 «소급 검사» 는 안 한다 — 한도_단위 12자 제한을
--    이미 위반한 살아있는 행이 있어(2026-09-06 실측 13행), 그 칸을 «안 건드리는»
--    UPDATE 까지 막히면 "아무것도 못 고친다" 가 실제로 벌어진다.
--    그래서 각 검사는 «이번에 그 칼럼이 바뀐 경우만» 본다 (IS DISTINCT FROM 가드).
-- ════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION corpus."f_rules_truncate_금지"() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  RAISE EXCEPTION
    '🔴 corpus.rules TRUNCATE 는 막혀 있다 (2026-09-06 오너 결정). '
    'seed_rules.py 의 rows() 는 2026-09-02 스냅샷이고 이후 DB 변경(배열 append 121건 · '
    'L1 단서 7행 · 사전승인_조건 UNION · 골든셋 판정 5건)이 거기 없다. '
    '재적재하면 그 전부가 되돌릴 수 없이 사라진다. '
    '정말 필요하면 사람이 DROP TRIGGER trg_rules_truncate_금지 ON corpus.rules; 를 «의도적으로» 친다.';
END $fn$;

CREATE OR REPLACE FUNCTION corpus.rules_guard() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    g jsonb;
    doc text;
    jo text;
    found boolean;
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

    -- ── 비목: `corpus.item_vocab` 정본 밖이면 거부 ──
    --    🔴 2026-09-06 — 하드코딩 10종에서 `item_vocab` «조회» 로 옮겼다.
    --       10종은 창업 계통뿐이라 TIPS(RND 8종)가 8행 중 7행 거부됐다. 그런데 명단을
    --       여기서 손으로 늘리면 «정본이 두 곳» 이 된다 — `item_vocab` 은 이미
    --       PK 가 (비목,계통) 으로 넓혀졌는데(5ca89ad) 여기만 확장 «전» 이었다.
    --       두 곳이면 반드시 갈린다. 그래서 출처를 하나로 모은다.
    --    🔴 검사를 «없앤» 게 아니다 — `item_vocab` 밖이면 여전히 거부된다.
    바뀐 := (TG_OP = 'INSERT') OR (NEW.비목 IS DISTINCT FROM OLD.비목);
    IF 바뀐 AND NOT EXISTS (SELECT 1 FROM corpus.item_vocab v WHERE v.비목 = NEW.비목) THEN
        RAISE EXCEPTION 'rules_guard: rule_id=% 비목 ''%'' 이 corpus.item_vocab 정본에 없다',
            NEW.rule_id, NEW.비목;
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
$fn$;

DROP TRIGGER IF EXISTS "trg_rules_truncate_금지" ON corpus.rules;
CREATE TRIGGER "trg_rules_truncate_금지" BEFORE TRUNCATE ON corpus.rules FOR EACH STATEMENT EXECUTE FUNCTION "f_rules_truncate_금지"();

DROP TRIGGER IF EXISTS "trg_rules_guard" ON corpus.rules;
CREATE TRIGGER trg_rules_guard BEFORE INSERT OR UPDATE ON corpus.rules FOR EACH ROW EXECUTE FUNCTION rules_guard();

