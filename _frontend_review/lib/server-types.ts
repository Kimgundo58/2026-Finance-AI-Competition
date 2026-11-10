"use client";

// 서버가 '실제로' 주는 모양. mock 서버(SUDDOE_MOCK=1) 응답을 직접 캡처해 적었습니다.
// 프론트 타입(types.ts)과 이름이 달라서 adapt.ts 가 사이에서 번역합니다.

export type 판정값 = "가능" | "조건부" | "불가" | "판단불가" | null;

/**
 * `corpus.precedence_rules` 한 행 — L1/L2/L3 중 어느 층이 이기는지.
 * `server/models.py::우선순위규칙` 과 1:1 대응 (2026-09-07, 서버 커밋 `7fcd665`).
 */
export type 우선순위규칙 = {
  우선계층: string;
  후순위계층: string;
  범위?: string | null;
  우선규범?: string | null;
  해석?: string | null;
};

/** `GET /api/programs` 항목 한 줄. `server/models.py::사업정보` 와 1:1 대응. */
export type 사업정보 = {
  사업명: string;
  별칭: string[];
  비목체계: string | null;
  트랙범위: string | null;
  우선순위규칙목록: 우선순위규칙[];
};

/** `GET /api/l3/current` 한 건. `server/models.py::L3현재문서` 와 1:1 대응. */
export type L3현재문서 = {
  doc_id: string;
  원본파일명: string;
  version: string | null;
  시행일: string | null;
  /** 등록한 날(YYYY-MM-DD). */
  등록일?: string | null;
  파싱품질: "pending" | "pass" | "warn" | "fail" | "review" | null;
  조_건수: number;
  /** `파싱품질="review"` 일 때 왜 확인이 필요한지. 사용자에게 보여 줄 문장입니다. */
  검토이유?: string | null;
  /** 텍스트를 얻은 방식. `"ocr"` 이면 스캔본을 OCR 로 읽은 문서입니다(금액·조번호 오인식 가능). */
  추출방식?: string | null;
  /**
   * 조문 연결(2026-11-07, P4-5). 판정에 쓰는 문서이고 서버가 연결을 쓸 때만 옵니다. `null` 이면 연결 화면을 보이지 않습니다.
   * `추천작성중` 이면 서버가 추천을 만드는 중입니다(잠시 뒤 다시 읽습니다). `추천없음` 은 추천을 만든 적이 없는 문서입니다(실패 아님).
   */
  연결상태?: "추천작성중" | "추천완료" | "추천일부실패" | "추천실패" | "추천없음" | null;
  연결?: L3연결조문[] | null;
  연결선택지?: L3연결선택지[] | null;
};

/** 고를 수 있는 분류. `concept_id` 는 저장할 때 돌려보내는 값이고 화면에 쓰지 않습니다. */
export type L3연결선택지 = { concept_id: string; 표시문: string };

/** 조문 하나의 연결. `server/models.py::L3연결조문항목`. `article_id`·`concept_ids` 는 화면에 쓰지 않습니다. */
export type L3연결조문 = {
  article_id: number;
  조번호: string;
  조제목: string | null;
  미리보기: string;
  /** `확인`: 저장함(판정이 읽음) · `추천`: 저장 전 추천 · `추천없음`: 추천을 만들지 못함. */
  상태: "확인" | "추천" | "추천없음";
  /** 지금 값(확인값, 없으면 추천). `[]` = 모든 지출에 공통, `null` = 고른 분류 없음. */
  concept_ids: string[] | null;
  표시문: string[];
  /** 대체된 옛 판 지침과 같고 지금 판에는 없는 문장. */
  옛판문구: { 문장: string; 문서: string }[];
};

/**
 * `POST /api/l3/upload`(202) · `GET /api/l3/{doc_id}` 응답. `server/models.py::L3업로드응답` 과 1:1 대응.
 *
 * 주의: 2026-11-03 — 기관당 기준 문서는 하나입니다. 새 문서를 읽으면 바로 그 문서로 판정하고,
 *    읽지 못하면 기존 문서를 계속 씁니다. `메시지` 가 두 경우를 이미 사용자 문장으로 담아 옵니다.
 */
export type L3처리결과 = {
  doc_id: string;
  파일명: string;
  확장자: string;
  /** `확인필요`: 다 읽었지만 세부기준이 맞는지 사용자가 골라야 합니다. `적용안함`: 쓰지 않기로 했습니다. */
  상태: "파싱대기" | "완료" | "실패" | "확인필요" | "적용안함" | (string & {});
  조_건수: number | null;
  /** 상위 규정을 찾지 못한 참조 목록. */
  미해결참조: Record<string, unknown>[];
  메시지: string | null;
  파싱품질: "pending" | "pass" | "warn" | "fail" | "review" | null;
  검토이유?: string | null;
  /** `상태="실패"` 일 때만. 스캔본·보호문서·조문없음·중단 등. 안내 문구는 `메시지` 에 있습니다. */
  실패사유?: string | null;
  /** 이 기관이 지금 판정에 쓰는 기준 문서 이름. `null` 이면 공통 관리 기준만으로 판정합니다. */
  적용중_문서?: string | null;
  /** `"ocr"` 이면 스캔본을 OCR 로 읽은 문서입니다. 이 문서를 근거로 한 '가능'은 서버가 '조건부'로 낮춥니다. */
  추출방식?: string | null;
};

/** GET /api/plans 의 항목 한 줄 */
export type 계획요약 = {
  plan_id: number;
  제목: string | null;
  확정비목: string | null;
  금액: number | null;
  판정: 판정값;
  집행예정일: string | null;
  updated_at: string | null;
  사업명: string | null;
  상태: "draft" | "judged";
  실제지출일?: string | null;
  실제지출액?: number | null;
  단계요약?: 단계항목[];
  현재단계?: string | null;
};

/** 지출 건의 진행 단계 한 칸. 서버가 할일에서 계산합니다. */
export type 단계항목 = {
  단계: string;
  상태: "완료" | "진행중" | "시작전" | "항목없음";
  완료: number;
  전체: number;
};

export type 계획통계 = {
  전체: number;
  확인필요: number;
  위험: number;
  특이사항없음: number;
  점검전: number;
  금액합계: number;
};

export type 계획목록응답 = {
  통계: 계획통계;
  건수: number;
  페이지: number;
  크기: number;
  항목: 계획요약[];
};

export type 할일 = {
  task_id: number;
  plan_id: number | null;
  출처: "ai" | "user";
  /** `corpus.check_items.code`. 매칭하지 못하면 null. */
  code: string | null;
  구분: "결제전" | "결제후" | "집행";
  항목: string;
  설명: string | null;
  due_date: string | null;
  유형: "기타" | "계약" | "비교견적" | "사전승인" | "사전확인" | "증빙";
  날짜_사용자수정: boolean;
  상태: "준비필요" | "집행예정" | "완료";
  계획제목?: string | null;
  /** true 면 이 일정의 날짜는 계획의 예상 지출일과 같습니다. 계획 수정으로만 바뀝니다. */
  집행예정일_연동?: boolean;
  /** 다른 구성원이 만든 일정이면 그 사람 이메일 앞부분. */
  작성자?: string | null;
};

export type 인용 = {
  문서?: string | null;
  조?: string | null;
  제목?: string | null;
  본문?: string | null;
  [k: string]: unknown;
};

export type 판정상세 = {
  조사?: import("./questions").조사결과 | null;
  판정?: 판정값;
  요약?: string | null;
  신뢰등급?: "A" | "B" | null;
  /** 인용한 규정의 버전 문자열. */
  규정버전?: string | null;
  인용?: 인용[];
  /** `전제[].매핑` 값의 프로필 경로 접두어는 아직 `F1.` 형식이며 이후 `협약.` 형식으로 바뀝니다. 프론트는 이 값을 해석하지 않습니다. */
  전제?: unknown[];
  /** 참조 관계 목록. 항목 키는 `참조문자열`·`해결근거` 등입니다. */
  참조관계?: unknown[];
  /** 판정을 하향하거나 경고한 검증 사유. */
  검증사유?: string[];
  /** 주의: 「판단불가」일 때만 포함됩니다. 아니면 키 자체가 없습니다. */
  문의초안?: string | null;
  [k: string]: unknown;
} | null;

export type 서버첨부 = {
  attachment_id: string;
  원본파일명: string;
  크기: number;
  task_id: number | null;
  created_at: string | null;
  /** 다른 구성원이 올렸으면 그 사람 이메일 앞부분. */
  작성자?: string | null;
};

/** `GET /api/team` 의 팀. `server/models.py::팀정보` 와 1:1 대응. */
export type 서버팀 = {
  팀이름: string;
  사업명: string;
  내역할: "대표" | "구성원";
  구성원: { account_id: string; 이메일: string; 나: boolean; 대표: boolean }[];
  /** 만들거나 재발급한 응답에만 있습니다. 서버가 해시만 저장해 다시 볼 수 없습니다. */
  초대코드?: string | null;
  초대코드_만료?: string | null;
};

export type 판정피드백 = {
  평가: "맞음" | "틀림";
  이유: "판정이 다름" | "근거가 다름" | "기타" | null;
  의견: string | null;
};

export type 계획상세 = 계획요약 & {
  질문원문: string | null;
  용도: string | null;
  거래처: string | null;
  추가설명: string | null;
  정규화: Record<string, unknown>;
  latest_decision_id: number | null;
  판정상세: 판정상세;
  할일: 할일[];
  created_at: string | null;
  /** 판정 전에 답한 내용. 옛 판정에는 없습니다. */
  사용자답변?: unknown;
  /** 판정이 어떤 기준 문서로 점검했는가. 값 형식은 `기준문서정리()` 가 확인합니다. */
  기준문서?: unknown;
  /** 지금 계정이 최신 판정에 남긴 평가. 재점검하면 새 판정이라 비어 있습니다. */
  내피드백?: 판정피드백 | null;
  /** 같은 판정에 다른 구성원이 남긴 평가. */
  팀평가?: (판정피드백 & { 작성자: string })[];
  첨부?: 서버첨부[];
};

export type 할일목록응답 = { 건수: number; 항목: 할일[] };

/**
 * 정규화가 돌려주는 비목 후보 한 건.
 *
 * 주의: `설명` 은 **아직 서버가 안 보냅니다** (`server/models.py::비목후보` 는 비목·신뢰도
 *    두 칸뿐). 백엔드가 컬럼을 추가하면 그때부터 값이 포함되어 오고 화면에 자동으로 표시됩니다
 *    — 그래서 선택 필드로 둡니다. 값이 없으면 설명 줄을 아예 안 그립니다.
 *    주의: 여기에 프론트가 지어낸 문구를 채워 넣지 마세요. 없는 근거를 만드는 것이고,
 *       서버가 진짜 값을 보내기 시작하면 둘이 어긋납니다.
 */
export type 비목후보 = { 비목: string; 신뢰도: number; 설명?: string | null };

/**
 * 심층질문 — `corpus.check_items` 기반, LLM 이 만들지 않는다(2026-09-06).
 * `models.py::심층질문항목` 과 1:1 대응. `code` 로 `/api/judge` 의 `답변` 에 되돌려 보낸다.
 */
export type 심층질문선택지 = { 값: string; 라벨: string };

export type 심층질문근거 = { doc_id: string; 조번호: string };

export type 심층질문항목 = {
  code: string;
  /** 이 질문이 속한 비목. 없으면 공통 질문입니다. */
  비목?: string | null;
  질문문: string;
  유형: "예아니오" | "선택" | "숫자" | "텍스트";
  /** `유형` 이 "선택" 일 때만 채워진다. */
  선택지: 심층질문선택지[];
  /** 왜 묻는지 — 화면에 조번호만 작게 보여준다. doc_id 원문은 노출하지 않는다. */
  근거: 심층질문근거;
  필요프로필필드: string[];
};

/** `/api/judge` 요청의 `답변[]` 한 칸 — `models.py::답변항목` 과 1:1 대응. */
export type 답변항목 = { code: string; 값: unknown };

/** `POST /api/questions` 질문의 선택지. `값` 은 예·아니오·모름 중 하나입니다. */
export type 확인질문선택지 = { 라벨: string; 값: string };

/** 질문의 근거 조문. `doc_id` 는 화면에 그대로 내지 않고 `문서명보기()` 로 바꿉니다. */
export type 확인질문근거 = {
  doc_id: string;
  조번호: string;
  조제목: string;
  항호: string;
  원문: string;
};

/** 판정 전에 묻는 질문 한 개. `id` 를 `/api/judge` 의 `답변[].code` 로 되돌려 보냅니다. */
export type 확인질문 = {
  id: string;
  질문문: string;
  이유: string;
  선택지: 확인질문선택지[];
  /** 원문을 받지 못하면 null 이고, 그때는 「규정 원문 보기」를 그리지 않습니다. */
  근거: 확인질문근거 | null;
};

/** 판정 결과의 `사용자답변[]` 한 칸. `답` 은 예·아니오·모름 중 하나입니다. */
export type 사용자답변항목 = { 질문문: string; 답: string };
/** 질문 화면의 선택 칸(거래 상대 등)에 답한 것. `문장` = 그 답으로 해당하지 않는다고 본 금지 기준. 답은 진술이라 판정 상한은 그대로입니다. */
/** 선택 칸 답. 문장 = 답으로 해당하지 않는다고 본 금지, 해당문장 = 답으로 해당한다고 본 금지(「이런 경우에는 안 돼요」에도 있음). 옛 판정에는 해당문장이 없어 빈 목록으로 읽습니다. */
export type 확인한조건항목 = { 항목: string; 답: string; 문장: string[]; 해당문장: string[] };

/** 입력한 금액의 부가세 기준. `server/models.py::부가세구분타입` 과 같습니다. */
export type 부가세구분 = "포함" | "공급가액" | "모름";

/**
 * 정규화가 돌려주는 분류 후보, 판정 결과의 `분류` 와 같은 모양입니다. 온톨로지 경로 서버만 보냅니다.
 * `표시문` 은 화면에 그대로 쓰는 말이고(「지급수수료 > 멘토링비」), 내부 ID 는 응답에 없습니다.
 */
export type 분류값 = { 비목: string; 세목: string | null; 표시문: string };

/** `POST /api/questions` 의 숫자·선택 칸. 코드가 한도 규범의 분모에서 만듭니다(`ontology_gate.py::추가입력_칸`). */
export type 추가입력칸 = {
  키: string;
  라벨: string;
  단위: string | null;
  형식: "숫자" | "선택";
  선택지?: string[];
};

/** 어려운 말 풀이 한 건. 질문 응답과 판정 결과가 같은 모양으로 줍니다. */
export type 용어풀이항목 = { 용어: string; 풀이: string };

/** LLM 없이 코드가 규범과 입력값을 비교한 결과. `비교값` · `한도` 는 화면 문구, `발췌` 는 규정 원문입니다. */
export type 코드판정값 = {
  종류: "금지" | "한도초과" | "부가세기준미정";
  비교값: string | null;
  한도: string | null;
  발췌: string | null;
};

export type 정규화결과 = {
  품목: string | null;
  금액: number | null;
  금액_추정여부: boolean;
  용도: string | null;
  비목후보: 비목후보[];
  하위항목: string | null;
  결제수단: string | null;
  구매명의: string | null;
  신청일: string | null;
  비교견적: string | null;
  질문원문: string | null;
  심층질문: 심층질문항목[];
};
