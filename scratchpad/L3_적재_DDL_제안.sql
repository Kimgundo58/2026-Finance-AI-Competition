-- 레인 L3 제안 — 실제 적용은 중앙(ai-8c)이 한다. 이 세션은 DB 쓰기 금지.
-- corpus.documents.status CHECK 에 'staged' 한 값만 추가한다. 기존 값(active·
-- superseded·reference)은 그대로 둔다 — 의미가 갈리는 다른 것이므로 재사용하지 않는다.

ALTER TABLE corpus.documents DROP CONSTRAINT documents_status_check;
ALTER TABLE corpus.documents ADD CONSTRAINT documents_status_check
    CHECK (status = ANY (ARRAY['active','superseded','reference','staged']));

-- 🔴 corpus.chunks.status 는 확인해보니 CHECK 제약이 «원래 없다»(자유 텍스트,
--    documents.status 와는 관례로만 맞춘다) — 여기는 DDL 이 필요 없다.
