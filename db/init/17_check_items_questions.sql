-- 서빙 코드가 읽지만 앞선 init 파일에 없던 컬럼을 추가한다.
-- 실행 시점: init (빈 볼륨에서 컨테이너를 처음 만들 때). 운영 DB 와 기존 로컬 DB 에는 이미 있으므로
-- 마이그레이션은 필요 없다. 전부 IF NOT EXISTS 라 다시 실행해도 같은 상태가 된다.
-- 이 컬럼이 없으면 심층질문이 빈 목록이 되고 해야할일 기한의 근거가 빠진다.

-- corpus.check_items: 심층질문(체크항목을 사용자에게 묻는 질문)과 기한 근거
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "질문문"        TEXT;
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "선택지"        JSONB;
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "질문유형"      TEXT;
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "필요f필드"     TEXT[];
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "기한근거"      TEXT;
ALTER TABLE corpus.check_items ADD COLUMN IF NOT EXISTS "기한근거_상세" TEXT;

-- eval.golden_set: 평가 문항 분류. 앱은 읽지 않고 평가 스크립트만 읽는다.
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS "대상"     TEXT;
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS "평가범위" TEXT;
ALTER TABLE eval.golden_set ADD COLUMN IF NOT EXISTS "채점모드" TEXT;
