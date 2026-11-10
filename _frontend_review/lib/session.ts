"use client";

/**
 * 토큰이 '어디서 오는가' 를 한 곳에서 정합니다.
 *
 * 두 가지 경로가 있고, 둘 다 씁니다 (2026-09-03 백엔드 확정).
 *
 *   Supabase 로그인        팀·실사용자
 *   POST /api/demo/session  심사위원 — 계정 없이 「둘러보기」
 *
 * 주의: 받은 뒤에는 '완전히 똑같이' 씁니다. `Authorization: Bearer <토큰>` 하나이고
 *    호출부는 어느 쪽인지 몰라도 됩니다. 그래서 `http.ts`·`sse.ts` 는 이 파일의
 *    `현재토큰()` 만 부릅니다.
 *
 * 주의: 데모 세션은 '누를 때마다 새 기관' 입니다. 고정 데모 기관 하나로 하면
 *    심사위원 A 의 지출계획이 심사위원 B 에게 보입니다(게스트 공용 버킷 문제가
 *    이름만 바꿔 되풀이됨). 그래서 재사용하지 않고, 만료되면 버립니다.
 *
 * 주의: `supabase.ts` 를 부르지만 그 반대는 없습니다. `http.ts` 도 이 파일을 부르고
 *    이 파일은 `http.ts` 를 안 부릅니다(직접 fetch). 순환 import 를 피합니다.
 */

import { apiBase, API켜짐 } from "./config";
import { 토큰 as supabase토큰 } from "./supabase";

const KEY = "checkumait-데모세션";

/** 만료 직전에 끊기지 않도록 30초 미리 버립니다. */
const 여유_ms = 30_000;

export type 데모세션 = {
  access_token: string;
  만료: number; // epoch ms
  기관명: string;
  slug: string;
};

function 읽기(): 데모세션 | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as 데모세션;
    if (!s?.access_token) return null;
    if (Date.now() + 여유_ms >= s.만료) {
      localStorage.removeItem(KEY);
      return null;
    }
    return s;
  } catch {
    return null;
  }
}

/** 지금 「둘러보기」로 들어와 있나. 화면이 배너를 띄우려면 이걸 봅니다. */
export function 데모중(): 데모세션 | null {
  return 읽기();
}

export function 데모종료(): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* 지우기 실패해도 만료되면 자연히 빠집니다 */
  }
}

/**
 * 「계정 없이 둘러보기」.
 *
 * 주의: 2026-09-03 현재 서버가 **503** 을 냅니다 (기관을 새로 만드는 순간 격리 규칙이
 *    막는 순환 의존 문제. 백엔드에서 수정 예정이었습니다). 응답 '형태' 는 확정이라
 *    바뀌지 않으므로, 200 이 되는 순간 코드 수정 없이 그대로 붙습니다.
 */
export async function 데모시작(): Promise<데모세션> {
  if (!API켜짐()) throw new Error("서버 주소가 설정되지 않았습니다.");

  let res: Response;
  try {
    res = await fetch(`${apiBase()}/api/demo/session`, { method: "POST" });
  } catch {
    throw new Error("서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요.");
  }

  if (!res.ok) {
    // 503 = 아직 준비 중. 사용자에게 「실패」가 아니라 「아직」으로 보여야 합니다.
    throw new Error(
      res.status === 503 || res.status === 404
        ? "둘러보기가 아직 준비되지 않았습니다. 잠시 뒤 다시 시도해 주세요."
        : `둘러보기를 시작하지 못했습니다 (${res.status}).`,
    );
  }

  const j = (await res.json()) as {
    access_token?: string;
    expires_in?: number;
    기관명?: string;
    slug?: string;
  };
  if (!j?.access_token) throw new Error("서버가 토큰을 주지 않았습니다.");

  const 세션: 데모세션 = {
    access_token: j.access_token,
    만료: Date.now() + (Number(j.expires_in) || 3600) * 1000,
    기관명: j.기관명 ?? "둘러보기",
    slug: j.slug ?? "",
  };
  try {
    localStorage.setItem(KEY, JSON.stringify(세션));
  } catch {
    /* 저장이 막혀도 이번 탭에서는 아래 메모리 값으로 동작합니다 */
    메모리 = 세션;
  }
  return 세션;
}

/** localStorage 가 막힌 브라우저(사생활 보호 모드 등) 대비. */
let 메모리: 데모세션 | null = null;

/* ── 로그인한 이메일 ───────────────────────────────────────────────
 * 주의: Supabase 값이 기준입니다. 다만 `인증켜짐=false` (설정 전 배포)일 때는
 *    로그인이 '통과만' 하므로 Supabase 에 아무것도 안 남습니다. 그때도 화면에
 *    실제 주소를 보여주려고, 로그인 폼이 넣은 값을 이 탭에만 적어 둡니다.
 *    세션 저장소라 탭을 닫으면 사라집니다 — 남길 값이 아닙니다. */
const 이메일KEY = "checkumait-로그인이메일";

export function 이메일기억(email: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(이메일KEY, email.trim());
  } catch {
    /* 막혀도 Supabase 쪽이 있으면 그걸 씁니다 */
  }
}

export function 기억된이메일(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.sessionStorage.getItem(이메일KEY) ?? "";
  } catch {
    return "";
  }
}

/* ── 팀 이름 ────────────────────────────────────────────────
 * 주의: 회원가입에서 받은 팀 이름. 우리 DB 에 담을 칸이 없어서 이 탭에도
 *    적어 둡니다. 마이페이지·사이드바가 「체쿠메이트」로 고정 표기되지 않게
 *    하려는 것이 전부입니다. */
const 팀KEY = "checkumait-팀이름";

export function 팀이름기억(이름: string): void {
  if (typeof window === "undefined") return;
  const 값 = (이름 ?? "").trim();
  if (!값) return;
  try {
    window.sessionStorage.setItem(팀KEY, 값);
  } catch {
    /* 무시 */
  }
}

export function 기억된팀이름(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.sessionStorage.getItem(팀KEY) ?? "";
  } catch {
    return "";
  }
}

export function 이메일잊기(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(이메일KEY);
    window.sessionStorage.removeItem(팀KEY);
  } catch {
    /* 무시 */
  }
}

/**
 * 주의: 요청에 넣을 토큰. **로그인한 사람이 먼저입니다.**
 *
 *    2026-09-04 뒤집었습니다. 전에는 데모 토큰을 먼저 썼는데, 예전에 「계정 없이
 *    둘러보기」를 한 번 누른 적이 있으면 그 토큰이 localStorage 에 2시간 남아서
 *    '로그인한 뒤에도' 데모 기관으로 조회됐습니다. 그러면 목록은 데모 기관 것이
 *    보이는데 그 사이 데모 org 가 정리되면 같은 계획을 열 때 404 가 납니다
 *    (「지출계획 23412 을(를) 찾을 수 없습니다」).
 *    로그인은 사용자가 '명시적으로' 한 행동이므로 그쪽을 믿습니다.
 */
export async function 현재토큰(): Promise<string | null> {
  const 로그인 = await supabase토큰();
  if (로그인) return 로그인;
  const d = 읽기() ?? (메모리 && Date.now() + 여유_ms < 메모리.만료 ? 메모리 : null);
  return d ? d.access_token : null;
}
