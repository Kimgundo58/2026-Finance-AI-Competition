"use client";

/**
 * 주관기관 검색 — `GET /api/orgs`
 *
 * 주의: 전에는 화면에 「경상국립대학교 창업중심대학사업단」 한 건이 하드코딩되어 있었습니다.
 *    서버에는 420건이 있는데 드롭다운에 다 넣을 수 없어서 임시로 줄여 둔 것이었고,
 *    그래서 '검색도 수정도 안 되는' 화면이 됐습니다. 이제 서버에 물어봅니다.
 *
 * 주의: 응답에 `org_id` 가 '없습니다'. 밖으로 나오는 식별자는 `slug`(역산 불가) 뿐이고,
 *    slug → org_id 는 서버 안에서만 풉니다. 프론트는 org_id 를 알 필요가 없습니다.
 */

import { GET, POST } from "./http";
import { API켜짐 } from "./config";

export type 기관 = {
  slug: string;
  기관명: string;
  사업명: string[];
};

type 기관목록응답 = {
  건수: number;
  페이지: number;
  크기: number;
  항목: 기관[];
};

/** 아직 아무것도 고르지 않았을 때 쓰는 기관명. */
export const 기본기관명 = "경상국립대학교 창업중심대학사업단";

/** 서버가 못 줄 때 화면이 비지 않게 하는 최소 목록. */
export const 예비기관: 기관 = {
  slug: "",
  기관명: 기본기관명,
  사업명: ["창업중심대학"],
};

/**
 * 주의: 온보딩에서 고른 '주관기관' 을 기억합니다.
 *
 *    예전에는 사이드바·홈·마이페이지 세 곳에 「경상국립대학교 창업중심대학사업단」이
 *    각각 하드코딩되어 있었습니다. 온보딩에서 다른 기관을 골라도 서비스 어디에도 안 나타나서,
 *    「고른 게 반영이 안 된다」로 보였습니다. 이제 한 곳에서 읽습니다.
 *
 * 주의: `선택사업()` 과 같은 규칙(localStorage)입니다 — 이 브라우저에만 남습니다.
 */
const 기관KEY = "checkumait-주관기관";

/**
 * 주의: **slug 도 같이 기억합니다** (2026-09-05).
 *
 *    가입한 사람을 서버 명부(`tenant.accounts`)에 올리려면 '어느 기관인가' 를
 *    말해야 하는데, 그 식별자가 slug 입니다 — org_id 는 프론트가 알 수도 없고
 *    알아서도 안 됩니다(`lib/http.ts` 주석 참조). 기관명은 동명이 있어서
 *    (「경상국립대학교」·「경상국립대학교 창업지원단」·「…창업중심대학사업단」)
 *    이름으로 되찾으면 '다른 기관에 붙습니다'.
 *
 * 주의: 온보딩이 이 값으로 `계정등록()` 을 부릅니다.
 */
const 기관SLUG_KEY = "checkumait-주관기관-slug";

export function 기관저장(이름: string, slug?: string): void {
  if (typeof window === "undefined") return;
  const 값 = (이름 ?? "").trim();
  if (!값) return;
  try {
    localStorage.setItem(기관KEY, 값);
    // 주의: slug 를 '안 받았을 때 지우지 않습니다'. 기존 호출부가 이름만 넘기는데
    //    거기서 지우면 어제 고른 기관의 slug 가 사라집니다.
    const s = (slug ?? "").trim();
    if (s) localStorage.setItem(기관SLUG_KEY, s);
  } catch {
    /* 저장이 막혀도 이번 세션은 그대로 동작합니다 */
  }
}

export type 계정등록결과 = {
  /** 계정이 실제로 속한 기관. 이미 등록된 계정이면 고른 기관과 다를 수 있습니다. */
  기관명: string;
  slug: string;
  새계정: boolean;
};

/**
 * 로그인한 계정을 고른 기관에 등록합니다 — `POST /api/accounts`.
 *
 * 주의: 이메일은 보내지 않습니다. 서버가 토큰에서 읽습니다. 등록이 안 된 계정은 다른 API 가
 *    전부 403 이라 기준 문서 업로드도 막힙니다. 이미 등록된 계정은 서버가 현재 기관을 돌려줍니다.
 */
/** 새 가입자가 초대 코드로 등록합니다 — `POST /api/accounts/join`. 기관과 사업은 팀의 것을 씁니다. */
export async function 초대코드등록(초대코드: string) {
  return (await POST("/api/accounts/join", { 초대코드 })) as {
    기관명: string;
    slug: string;
    팀이름: string;
    사업명: string;
  };
}

export async function 계정등록(slug: string): Promise<계정등록결과> {
  return (await POST("/api/accounts", { slug })) as 계정등록결과;
}

export function 선택기관(): string {
  if (typeof window === "undefined") return 기본기관명;
  try {
    return localStorage.getItem(기관KEY)?.trim() || 기본기관명;
  } catch {
    return 기본기관명;
  }
}

/**
 * 주의: 2026-09-07 — 「지금 적용 중인 세부기준 파일」고정 표기를 여기서 뺐습니다.
 *    `GET /api/l3/current` 가 신설되어(서버 커밋 `7fcd665`) 실제 org 문서를
 *    돌려주므로, 화면은 이제 `lib/api.ts::L3현재문서목록()` →
 *    `lib/adapt.ts::L3현재문서요약짓기()` 를 씁니다(`app/checkumait-app.tsx`
 *    의 `use현재기준문서()`). 이 파일은 더 이상 파일명을 지어내지 않습니다.
 */

export type 검색결과 = {
  항목: 기관[];
  건수: number;
  /** 주의: 서버를 못 불러 예비 목록으로 답했는가. 화면이 「검색이 안 된다」를 알 수 있게. */
  폴백: boolean;
  /**
   * 주의: 사업으로 걸렀더니 0건이라 '필터를 풀고' 전체에서 답했는가.
   *    화면이 그 사실을 말해야 합니다 — 안 그러면 사용자는 자기 사업의 기관 목록을
   *    보고 있다고 믿습니다.
   */
  필터해제?: boolean;
};

async function 한번검색(
  q: string,
  사업명: string | undefined,
  크기: number,
): Promise<기관[] | null> {
  try {
    const r = (await GET("/api/orgs", {
      q: q.trim() || undefined,
      사업명,
      크기,
    })) as 기관목록응답;
    return Array.isArray(r?.항목) ? r.항목 : [];
  } catch {
    return null; // 주의: 「0건」과 「서버가 안 됨」은 다릅니다
  }
}

/**
 * 기관명 부분일치 검색. 서버가 공백을 무시하고 맞춥니다.
 *
 * @param q      검색어. 비어 있으면 앞에서부터 `크기` 건.
 * @param 사업명  주면 그 사업을 운영하는 기관만 (`orgs.사업명` 배열에 정확히 있는 것).
 *
 * 주의: 사업으로 걸렀는데 0건이면 '필터를 풀고' 다시 찾습니다. 그 사업으로 등록된
 *    기관이 DB 에 아직 없을 수 있는데, 그때 빈 목록을 주면 온보딩 2단계에서
 *    아무것도 못 고르고 막힙니다. 대신 `필터해제: true` 로 알려서 화면이 말하게 합니다.
 */
export async function 기관검색(
  q: string,
  옵션: { 사업명?: string; 크기?: number; signal?: AbortSignal } = {},
): Promise<검색결과> {
  const 예비 = { 항목: [예비기관], 건수: 1, 폴백: true };
  if (!API켜짐()) return 예비;
  const 크기 = 옵션.크기 ?? 20;
  const 사업명 = 옵션.사업명?.trim() || undefined;

  const 걸러낸것 = await 한번검색(q, 사업명, 크기);
  if (걸러낸것 === null) return 예비;              // 서버가 안 됨
  if (걸러낸것.length) {
    return { 항목: 걸러낸것, 건수: 걸러낸것.length, 폴백: false };
  }

  // 사업 필터 없이 찾은 것도 0건이면 '정말 없는' 것입니다.
  if (!사업명) {
    // 주의: 검색어 없이 0건 = 서버에 기관 데이터가 없는 상태. 화면을 막지 않습니다.
    if (!q.trim()) return 예비;
    return { 항목: [], 건수: 0, 폴백: false };
  }

  const 전체 = await 한번검색(q, undefined, 크기);
  if (전체 === null) return 예비;
  if (!전체.length) {
    if (!q.trim()) return 예비;
    return { 항목: [], 건수: 0, 폴백: false };
  }
  return { 항목: 전체, 건수: 전체.length, 폴백: false, 필터해제: true };
}

/** 기관명 아래 회색 줄에 쓸 한 줄 설명. 사업이 없으면 빈 문자열. */
export function 사업요약(기관: 기관): string {
  const 목록 = (기관.사업명 ?? []).filter(Boolean);
  if (!목록.length) return "";
  return 목록.length <= 2 ? 목록.join(" · ") : `${목록.slice(0, 2).join(" · ")} 외 ${목록.length - 2}건`;
}
