"use client";

// 화면이 부르는 함수는 전부 여기 있습니다. 주소를 아는 곳은 config.ts 하나입니다.

import { GET, POST, PATCH, PUT, DELETE, 인증헤더, 응답처리 } from "./http";
import type { 서버팀,
  서버첨부,
  판정피드백,
  계획목록응답,
  계획상세 as 계획상세형,   // 아래 같은 이름의 '함수' 와 겹쳐서 별칭으로 받습니다
  할일,
  할일목록응답,
  사업정보,
  L3현재문서,
  L3처리결과,
} from "./server-types";

/* ── 사업 · 비목 ─────────────────────────────── */

/**
 * 화면 2 온보딩① 사업 선택 · 마이페이지 「공통 관리 기준」.
 * 주의: 2026-09-07 — `사업정보.우선순위규칙목록` 이 추가됐습니다(서버 커밋 `7fcd665`,
 *    `corpus.precedence_rules`, verified 행만). `lib/norms.ts` 가 이걸 읽습니다.
 */
export const 사업목록 = () =>
  GET("/api/programs") as Promise<{ 사업: 사업정보[]; 비고?: string | null }>;

/**
 * 이 기관(주관기관)에 '지금 적용 중'(status='active')인 L3 문서 목록.
 * 주의: 2026-09-07 신설(`GET /api/l3/current`) — `lib/orgs.ts:107 적용중_기준파일`
 *    하드코딩 대체용.
 *
 * 주의: `org_id` 를 인자로 받지 않습니다 — 여기서 프론트가 알 수 있는 식별자는
 *    `slug`(HMAC, 역산 불가) 뿐이고 `org_id` 는 UUID 여야 하니 slug 를 넣으면
 *    422 로 거부됩니다(`server/auth.py::_uuid인가`). org 는 로그인 토큰에서
 *    미들웨어가 정합니다(`server/auth.py` §org_id 주입) — 게스트면 서버가 빈
 *    목록을 줍니다.
 */
export const L3현재문서목록 = () => GET("/api/l3/current") as Promise<{ 문서: L3현재문서[] }>;

/**
 * 화면 9 비목 확인.
 * 주의: 비목 라벨을 화면에 '하드코딩하지 마세요' — 이 응답이 기준 데이터입니다.
 *    사업명을 주면 걸러집니다 (창업활동비는 예비창업패키지에만 있습니다).
 */
export const 비목목록 = (사업명?: string) =>
  GET("/api/vocab", { 사업명 }) as Promise<{
    비목: string[];
    별칭: Record<string, string[]> | null;
  }>;

/* ── 지출 계획 ───────────────────────────────── */

export const 계획목록 = (opt: {
  탭?: string; 사업명?: string; 확정비목?: string; q?: string;
  금액_최소?: number; 금액_최대?: number;
  정렬?: string; 페이지?: number; 크기?: number;
} = {}) => GET("/api/plans", opt) as Promise<계획목록응답>;

export const 계획상세 = (planId: number | string) =>
  GET(`/api/plans/${planId}`) as Promise<계획상세형>;

/**
 * 화면 11 「삭제」 — v26 배포로 신설(책임자 요청, 2026-09-07).
 * 주의: 계획을 지워도 **판정 기록(`decisions`)은 안 지웁니다** — 감사 흔적으로 연결만
 *    끊습니다. `할일삭제` 는 같이 지워진 확인 항목·일정(plan_tasks) 개수입니다.
 * 주의: 없거나 남의 org 계획이면 서버가 403 이 아니라 **404** 를 줍니다(존재 자체를
 *    숨기려는 설계) — 호출부가 이걸 「이미 지워졌나 보다」 로 읽으면 됩니다.
 */
export const 계획삭제 = (planId: number | string) =>
  DELETE(`/api/plans/${planId}`) as Promise<{ 삭제: boolean; plan_id: number; 제목: string | null; 할일삭제: number }>;

/** 화면 8 저장. 사업명·품목·금액·용도가 '필수' 입니다. */
export const 계획추가 = (입력: {
  사업명: string; 품목: string; 금액: number; 용도: string;
  제목?: string; 집행예정일?: string; 거래처?: string; 추가설명?: string;
  확정비목?: string; 질문원문?: string; 정규화?: unknown;
}) => POST("/api/plans", 입력) as Promise<계획상세형>;

/**
 * 예상 지출일을 바꿉니다. `일정이동` 이 true 면 완료 안 된 연결 일정도 같은 일수만큼 옮깁니다.
 * 주의: 판정이 집행예정일(서버 `사용일자`)을 읽습니다. 프로필의 협약 기간 밖이면 코드 불가입니다. 저장한 판정은 그대로이니 호출한 쪽이
 * 이미 점검한 계획이면 「재점검 필요」로 표시해야 합니다.
 */
export const 계획수정 = (planId: number | string, 입력: {
  집행예정일?: string | null;
  일정이동?: boolean;
  실제지출일?: string;
  실제지출액?: number;
}) => PATCH(`/api/plans/${planId}`, 입력) as Promise<계획상세형>;

/* ── 할 일 ──────────────────────────────────── */

export const 할일추가 = (planId: number | string, 입력: {
  항목: string; 설명?: string;
  // 주의: 서버 `models.할일생성` 은 셋을 받습니다 (결제전·결제후·집행). 여기가 둘만
  //    적어 두면 「집행」 일정이 조용히 「결제후」로 접혀 나갑니다.
  구분?: "결제전" | "결제후" | "집행";
  due_date?: string;
  유형?: 할일["유형"];
  상태?: 할일["상태"];
}) => POST(`/api/plans/${planId}/tasks`, 입력) as Promise<할일>;

/** 지출계획에 묶이지 않은 일정을 만듭니다. */
export const 일정추가 = (입력: {
  항목: string; 설명?: string;
  구분?: 할일["구분"];
  due_date?: string;
  유형?: 할일["유형"];
  상태?: 할일["상태"];
}) => POST("/api/tasks", 입력) as Promise<할일>;

/**
 * 집행 일정 화면의 수정. 계획 없는 일정도 같은 경로를 씁니다. 보낸 값만 바뀝니다.
 * 주의: `due_date: null` 은 날짜를 비웁니다(「날짜 미정」으로 옮겨 갑니다).
 */
export const 일정수정 = (taskId: number | string, 입력: {
  항목?: string; 설명?: string | null;
  구분?: 할일["구분"];
  due_date?: string | null;
  유형?: 할일["유형"];
  상태?: 할일["상태"];
}) => PATCH(`/api/tasks/${taskId}`, 입력) as Promise<할일>;

/** 지출 계획에 파일 하나를 저장합니다. taskId 를 주면 그 증빙 항목에 묶입니다. */
export async function 첨부업로드(planId: string, 파일: File, taskId?: string): Promise<서버첨부> {
  const fd = new FormData();
  fd.append("파일", 파일);
  if (taskId) fd.append("task_id", taskId);
  const { apiBase } = await import("./config");
  const res = await fetch(`${apiBase()}/api/plans/${planId}/attachments`, {
    method: "POST",
    headers: await 인증헤더(),
    body: fd,
    signal: AbortSignal.timeout(60_000),
  });
  return 응답처리(res) as Promise<서버첨부>;
}

export async function 첨부파일받기(planId: string, attachmentId: string): Promise<Blob> {
  const { apiBase } = await import("./config");
  const res = await fetch(`${apiBase()}/api/plans/${planId}/attachments/${attachmentId}`, {
    headers: await 인증헤더(),
  });
  if (!res.ok) await 응답처리(res);
  return res.blob();
}

export const 첨부삭제 = (planId: string, attachmentId: string) =>
  DELETE(`/api/plans/${planId}/attachments/${attachmentId}`) as Promise<{ 삭제: boolean }>;

/* ── 팀 ── 기업 데이터를 함께 쓰는 계정 묶음. 조회만 팀이 없을 때 `팀: null` 입니다. */
type 팀응답 = { 팀: 서버팀 | null };
export const 팀조회 = () => GET("/api/team") as Promise<팀응답>;
export const 팀만들기 = (팀이름: string, 사업명: string) =>
  POST("/api/team", { 팀이름, 사업명 }) as Promise<팀응답>;
export const 초대코드재발급 = () => POST("/api/team/invite-code", {}) as Promise<팀응답>;
export const 팀들어가기 = (초대코드: string) => POST("/api/team/join", { 초대코드 }) as Promise<팀응답>;
export const 팀나가기 = () => POST("/api/team/leave", {}) as Promise<팀응답>;
export const 팀원내보내기 = (accountId: string) =>
  DELETE(`/api/team/members/${accountId}`) as Promise<팀응답>;

/** 판정 결과의 「맞았어요 / 틀렸어요」. 같은 판정에 다시 보내면 바뀝니다. */
export const 피드백저장 = (decisionId: number, 입력: 판정피드백) =>
  PUT(`/api/decisions/${decisionId}/feedback`, 입력) as Promise<판정피드백>;

/** 사용자가 만든 일정만 지워집니다. 규정 점검이 만든 일정은 서버가 거절합니다(409). */
export const 일정삭제 = (taskId: number | string) =>
  DELETE(`/api/tasks/${taskId}`) as Promise<{ 삭제: boolean; task_id: number }>;

export const 할일수정 = (
  planId: number | string,
  taskId: number | string,
  입력: {
    상태?: 할일["상태"];
    due_date?: string;
    유형?: 할일["유형"];
  },
) => PATCH(`/api/plans/${planId}/tasks/${taskId}`, 입력) as Promise<할일>;

/** 집행 일정 화면. 일정만=true 면 날짜가 있는 것만 옵니다. */
export const 할일목록 = (opt: {
  상태?: string; 구분?: string; plan_id?: number;
  일정만?: boolean; 이후?: string;
} = {}) => GET("/api/tasks", opt) as Promise<할일목록응답>;

/* 주의: tasks:sync 는 '부르지 마세요'. 판정 직후 서버가 자동으로 넣습니다.
      프론트가 또 부르면 할일이 중복됩니다 (계약 문서 명시).             */

/* ── 내 정보 (화면 14) ───────────────────────── */

/** 기업 프로필. 키는 `협약`(협약 정보)·`집행내역`·`참여인력` 입니다. */
export const 프로필읽기 = (사업명?: string) =>
  GET("/api/profile", 사업명 ? { 사업명 } : undefined) as Promise<Record<string, unknown>>;
/** 주의: `사업명` 은 응답의 `협약경고` 가 어느 사업의 규정을 볼지만 고릅니다. 서버는 저장하지 않습니다. */
export const 프로필저장 = (p: { 협약?: unknown; 집행내역?: unknown[]; 참여인력?: unknown[] }, 사업명?: string) =>
  PUT("/api/profile", p, 사업명 ? { 사업명 } : undefined) as Promise<Record<string, unknown>>;

/* ── 기관 규정 업로드 (화면 4) ───────────────── */

/**
 * 주의: 여기만 `org_id` 를 아직 '본문(FormData)' 으로 보냅니다 — URL 이 아닙니다.
 *    서버 `routes_l3.업로드` 가 `org_id: str = Form(...)` 로 '필수' 폼필드를 받고,
 *    토큰 주입 미들웨어는 쿼리스트링만 교체하지 멀티파트 본문은 안 건드립니다.
 *    그래서 지금 빼면 업로드가 422 로 실패합니다.
 *    → 서버가 폼필드 대신 토큰에서 org_id 를 읽게 바뀌면 이 인자도 지웁니다. (백엔드 몫)
 *    URL·히스토리에는 남지 않으므로 이번 변경의 목적(주소창 누출 차단)은 충족됩니다.
 *
 * 주의: 이 함수는 `http.ts` 를 안 거치는 '직접 fetch' 라 그동안 Authorization 이
 *    아예 안 붙었습니다. 붙입니다.
 */
export async function 규정업로드(파일: File, 기관명: string, orgId?: string) {
  // 주의: 2026-09-06 — `orgId` 를 '선택' 으로 바꿉니다. 프론트는 org_id 를 알 수 없고
  //    (`lib/orgs.ts:10`), 서버가 토큰 주체로 정합니다(`routes_l3._업로드_주인` — 토큰이 우선합니다).
  //    필수로 두는 바람에 화면에서 이 함수를 '아예 부를 수 없었습니다'.
  const fd = new FormData();
  fd.append("파일", 파일);
  if (orgId) fd.append("org_id", orgId);
  fd.append("기관명", 기관명);
  // 주의: FormData 에는 Content-Type 을 직접 넣지 마세요 — 브라우저가 경계문자를 붙입니다
  const { apiBase } = await import("./config");
  const res = await fetch(`${apiBase()}/api/l3/upload`, {
    method: "POST",
    headers: await 인증헤더(),          // 게스트면 빈 객체 — 헤더가 아예 안 붙습니다
    body: fd,
    // 주의: 응답이 멈추면 화면의 「읽는 중」 창이 닫히지 않습니다. 30MB 를 느린 망으로 올려도
    //    충분한 3분을 상한으로 둡니다(넘으면 TimeoutError — 호출부가 사용자 문장으로 바꿉니다).
    signal: AbortSignal.timeout(180_000),
  });
  // 주의: 202 응답이라 `상태` 는 거의 늘 '파싱대기' 입니다 — 결과는 `규정처리기다리기()` 로 받습니다.
  return 응답처리(res) as Promise<L3처리결과>;
}

/**
 * 주의: 2026-09-03 — `org_id` 인자를 '뺐습니다'. 서버가 토큰에서 기관을 정합니다.
 *    (그전에는 URL 에 `?org_id=` 로 포함되어 브라우저 히스토리에 남았습니다.)
 *    로그인 없이 부르면 게스트로 조회되어 남의 문서는 보이지 않습니다.
 */
export const 규정상태 = (docId: string) => GET(`/api/l3/${docId}`) as Promise<L3처리결과>;

/**
 * 업로드한 기준 문서를 서버가 다 읽을 때까지 상태를 물어 결과를 돌려줍니다.
 *
 * 주의: 2026-11-03 — 업로드는 202(접수)만 돌려주고 파싱은 서버 뒤에서 돕니다. 그동안 화면은
 *    접수 응답만 보고 「등록했습니다」를 띄워서, 스캔본처럼 읽지 못한 문서도 적용된 것처럼
 *    보였습니다. `상태` 가 '파싱대기' 를 벗어나면(완료·실패) 바로 돌려줍니다. `최대ms` 안에 안
 *    끝나면 마지막 응답(아직 '파싱대기')을 돌려줍니다 — 그 `메시지` 가 "끝날 때까지는 기존 규정으로
 *    판정합니다" 를 담고 있습니다. 조회가 한 번도 안 되면 접수 응답을 그대로 돌려줍니다.
 */
export async function 규정처리기다리기(
  접수: L3처리결과,
  최대ms = 120_000,
  간격ms = 1_500,
): Promise<L3처리결과> {
  let 마지막 = 접수;
  const 끝 = Date.now() + 최대ms;
  while (L3처리중(마지막.상태) && Date.now() < 끝) {
    await new Promise((r) => setTimeout(r, 간격ms));
    try {
      // 주의: 응답이 멈춘 요청 하나가 전체 대기를 붙잡지 않게 한 번의 조회에도 상한을 둡니다
      마지막 = await Promise.race([
        규정상태(접수.doc_id),
        new Promise<never>((_, 거절) =>
          setTimeout(() => 거절(new Error("상태 조회 응답이 늦습니다")), 15_000),
        ),
      ]);
    } catch (e) {
      // 401·403·404 는 다시 물어도 같습니다 — 2분을 채우지 않고 바로 알립니다.
      // 그 밖(네트워크·5xx·지연)은 일시적일 수 있어 다음 차례에 다시 묻습니다.
      const 상태코드 = (e as { status?: number } | null)?.status;
      if (상태코드 === 401 || 상태코드 === 403 || 상태코드 === 404) throw e;
    }
  }
  return 마지막;
}

/** 서버가 아직 읽고 있는 상태인가. 서버 계약상 '파싱중' 도 올 수 있습니다(`server/models.py::L3업로드응답`). */
/** 확인을 기다리는 문서를 세부기준으로 씁니다. 이 문서가 판정 기준이 되고 이전 문서는 내려갑니다. */
export const 규정적용 = (docId: string) => POST(`/api/l3/${docId}/confirm`, {}) as Promise<L3처리결과>;

/** 확인을 기다리는 문서를 쓰지 않습니다. 판정 기준은 그대로입니다. */
export const 규정거절 = (docId: string) => POST(`/api/l3/${docId}/reject`, {}) as Promise<L3처리결과>;

export const L3처리중 = (상태: string) => 상태 === "파싱대기" || 상태 === "파싱중";

/**
 * 조문 연결 저장(2026-11-07, `PATCH /api/l3/{doc_id}/links`). 값: `[concept_id]` · `[]`(모든 지출에 공통) · `null`(확인 안 함).
 * 같은 기관의 판정에 쓰는 문서만 저장됩니다. 고를 수 없는 분류가 섞이면 하나도 저장하지 않고 400 입니다.
 */
export const 조문연결저장 = (docId: string, 값: Record<string, string[] | null>) =>
  PATCH(`/api/l3/${docId}/links`, 값) as Promise<L3현재문서>;

/* ── 서버 살았나 ─────────────────────────────── */

export const 헬스 = () =>
  GET("/api/health") as Promise<{ ok: boolean; 모드: "mock" | "real" }>;
