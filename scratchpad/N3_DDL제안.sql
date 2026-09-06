-- N3 증빙 발급처 URL — 제안 DDL. DB 쓰기는 중앙(ai-8c)이 한다. 여기선 실행하지 않았다.

-- ① 칼럼 추가
ALTER TABLE corpus.evidence_sources ADD COLUMN IF NOT EXISTS 발급처_url TEXT;
COMMENT ON COLUMN corpus.evidence_sources.발급처_url IS
  '발급처 원문에서 뽑은 URL(https:// 정규화). 원문에 도메인이 없으면 NULL — 지어내지 않는다. '
  '2026-09-06 ai-14 추출, scratchpad/N3_발급처URL.json 이 원본.';

-- ② 값 채우기 — scratchpad/N3_발급처URL.json 의 {증빙명, 뽑은url} 그대로.
--    증빙명이 corpus.evidence_sources 의 사실상 유일키라 그걸로 UPDATE 한다.
--    (참고: 아래는 예시 3건만 — 전체 96건은 JSON 을 순회해 생성해야 한다. 손으로 다 안 쳤다.)
UPDATE corpus.evidence_sources SET 발급처_url = 'https://4insure.or.kr'
  WHERE 증빙명 = '4대보험가입증명원';
UPDATE corpus.evidence_sources SET 발급처_url = 'https://iris.go.kr'
  WHERE 증빙명 = '거래명세서';
UPDATE corpus.evidence_sources SET 발급처_url = 'https://patent.go.kr'
  WHERE 증빙명 = '관납료 영수증';
-- ... (나머지는 scratchpad/N3_발급처URL.json 의 96건을 코드로 순회해 적재 권장 — 96줄을 손으로
--      더 치면 오타가 인용 오류가 된다. 이미 db.connect() 로 UPDATE 문을 자동 생성할 수 있다.)

-- ③ 없는 건 그대로 NULL (별도 UPDATE 불필요 — 컬럼 기본값이 NULL)
