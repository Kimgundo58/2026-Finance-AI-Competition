-- 프론트 표기를 corpus.programs.별칭 에 흡수한다. 접두사·공백 정규화로는 안 붙는 별개 이름들이다.
-- 정본(corpus.chunks.사업명 8종)은 바꾸지 않는다 — 조인 키로 널리 쓰인다. 별칭은 덧붙이는 것뿐이다.
-- 가운뎃점 코드포인트에 주의: 모두의창업 쪽은 U+30FB, TIPS 쪽은 U+00B7 다 — 정규화로도 안 합쳐져 원문 그대로 넣는다.

UPDATE corpus.programs SET "별칭" = "별칭" || ARRAY['모두의 창업 일반・기술']
 WHERE "사업명" = '모두의 창업 프로젝트'
   AND NOT ('모두의 창업 일반・기술' = ANY("별칭"));

UPDATE corpus.programs SET "별칭" = "별칭" || ARRAY['초격차 스타트업 1000+']
 WHERE "사업명" = '초격차 스타트업 프로젝트'
   AND NOT ('초격차 스타트업 1000+' = ANY("별칭"));

UPDATE corpus.programs SET "별칭" = "별칭" || ARRAY['민관공동 창업자 발굴·육성']
 WHERE "사업명" = 'TIPS'
   AND NOT ('민관공동 창업자 발굴·육성' = ANY("별칭"));
