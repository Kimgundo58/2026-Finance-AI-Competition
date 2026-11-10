"use client";

// 할일(체크박스·집행 일정) → 서버 저장
//
// 주의: `tasks:sync` 는 '부르지 않습니다'. 판정 직후 서버가 자동으로 넣습니다.

import { API켜짐 } from "./config";
import { 일정수정, 일정추가, 일정삭제, 할일수정, 할일추가 } from "./api";
import { 인증오류 } from "./http";
import type { 할일 } from "./server-types";
import type { ScheduleItem } from "./types";

/** 숫자 id 만 서버의 진짜 task_id 입니다. 판정에서 갓 나온 항목은 `ai-0` 같은 임시 id 라 건너뜁니다. */
export function 서버항목인가(id: string): boolean {
  return /^\d+$/.test(id);
}

/**
 * 저장 결과.
 *
 * 주의: `없음` 을 '실패' 와 구분하는 게 이 타입의 이유입니다.
 *    서버는 `WHERE task_id=? AND plan_id=? AND <org조건>` 이 0행이면 404 로
 *    「할일 123 을(를) 찾을 수 없습니다」를 돌려줍니다(`routes_tasks._live_수정`).
 *    그 문구를 그대로 사용자에게 띄우면 '고장'처럼 보이는데, 실제로는 화면이 가진
 *    목록이 서버보다 오래된 것입니다 — 새로 읽으면 해결됩니다.
 */
export type 저장결과 = "저장됨" | "건너뜀" | "없음" | "실패";

function 없음인가(e: unknown): boolean {
  return e instanceof Error && /찾을 수 없습니다|not found/i.test(e.message);
}

/**
 * 체크 상태를 서버에 저장합니다.
 *
 * @param 서버할일  이 계획이 '서버에서 받아온' 할일 id 집합. 주면 그 안에 없는 id 는
 *                 아예 요청을 안 보냅니다 — 낡은 화면이 404 를 만들지 않게.
 */
export async function 체크저장(
  planId: string,
  taskId: string,
  완료: boolean,
  서버할일?: ReadonlySet<string>,
): Promise<저장결과> {
  if (!API켜짐()) return "건너뜀";
  if (!서버항목인가(planId) || !서버항목인가(taskId)) return "건너뜀";
  if (서버할일 && !서버할일.has(taskId)) return "건너뜀";

  try {
    // 주의: 상태 어휘는 DB CHECK 로 닫혀 있습니다: 준비필요 · 집행예정 · 완료
    await 할일수정(planId, taskId, { 상태: 완료 ? "완료" : "준비필요" });
    return "저장됨";
  } catch (e) {
    if (없음인가(e)) return "없음";
    throw e;
  }
}

/* ── 집행 일정 ───────────────────────────────── */

/**
 * 화면의 일정 상태 → 서버 `plan_tasks.상태`.
 * 주의: 세 값 밖으로 나가면 DB CHECK 가 막습니다.
 */
const 상태사전: Record<ScheduleItem["state"], "준비필요" | "집행예정" | "완료"> = {
  "준비 필요": "준비필요",
  "집행 예정": "집행예정",
  완료: "완료",
};

/**
 * 일정 하나에서 바뀐 값만 서버에 저장합니다. 계획 없는 일정도 같은 경로를 씁니다.
 * 서버 행이 아닌 일정(저장 전 임시 id)은 건너뜁니다.
 */
export async function 일정저장(이전: ScheduleItem, 다음: ScheduleItem): Promise<저장결과> {
  if (!API켜짐()) return "건너뜀";
  if (!서버항목인가(다음.id)) return "건너뜀";
  const 바뀐값: Parameters<typeof 일정수정>[1] = {};
  if (이전.state !== 다음.state) 바뀐값.상태 = 상태사전[다음.state];
  if (이전.date !== 다음.date) 바뀐값.due_date = 다음.date || null;
  if (이전.title !== 다음.title) 바뀐값.항목 = 다음.title;
  if ((이전.memo ?? "") !== (다음.memo ?? "")) 바뀐값.설명 = 다음.memo?.trim() || null;
  if (이전.type !== 다음.type) {
    바뀐값.유형 = 유형사전[다음.type];
    바뀐값.구분 = 구분추정(다음.type);
  }
  if (!Object.keys(바뀐값).length) return "건너뜀";
  try {
    await 일정수정(다음.id, 바뀐값);
    return "저장됨";
  } catch (e) {
    if (없음인가(e)) return "없음";
    throw e;
  }
}

/** 규정 점검이 만든 일정인가. 재점검이 제목·메모를 다시 쓰고 항목을 다시 만들어, 날짜와 상태만 바꿀 수 있습니다. */
export function 점검일정인가(item: ScheduleItem): boolean {
  return !item.지출일연동 && (item.출처 === "ai" || (item.출처 !== "user" && !!item.taskId));
}

/** 일정을 서버에서 지웁니다. 서버에 이미 없으면 지워진 것으로 봅니다. */
export async function 일정지우기(item: ScheduleItem): Promise<void> {
  if (!API켜짐() || !서버항목인가(item.id)) return;
  try {
    await 일정삭제(item.id);
  } catch (e) {
    if (!없음인가(e)) throw e;
  }
}

/**
 * 이전/다음 일정 목록을 견줘서 '바뀐 것'만 서버에 보냅니다.
 * 돌려주는 값은 저장에 실패한 일정들입니다 (호출부가 화면을 되돌립니다).
 */
export async function 일정변경저장(
  이전: ScheduleItem[],
  다음: ScheduleItem[],
): Promise<{ 실패: ScheduleItem[]; 없음: ScheduleItem[] }> {
  const 실패: ScheduleItem[] = [];
  const 없음: ScheduleItem[] = [];
  if (!API켜짐()) return { 실패, 없음 };

  const 전 = new Map(이전.map((s) => [s.id, s]));

  await Promise.all(
    다음.map(async (s) => {
      const p = 전.get(s.id);
      if (!p || p === s) return;
      try {
        const r = await 일정저장(p, s);
        if (r === "없음") 없음.push(s);
      } catch {
        실패.push(s);
      }
    }),
  );
  return { 실패, 없음 };
}

/* ── 일정 '새로' 만들기 ─────────────────────────────
 *
 * 주의: 2026-09-05 연동. 그동안 사용자가 집행 일정에서 직접 추가한 항목은
 *    화면에만 있고 새로고침하면 사라졌습니다. 서버에는 `POST /api/plans/{id}/tasks`
 *    가 '이미' 있었고(`routes_tasks.py:151`, 201) `lib/api.ts::할일추가` 래퍼도
 *    있었는데 부르는 곳이 0곳이었습니다 — 없는 기능이 아니라 '안 부른' 것입니다.
 */

/**
 * 화면의 일정 종류 → 서버 `plan_tasks.유형`. 주의: 이 값들 밖은 DB CHECK 가 막습니다.
 * 「집행」은 유형이 아니라 `구분='집행'` 으로 저장합니다(`구분추정`).
 */
const 유형사전: Record<ScheduleItem["type"], 할일["유형"]> = {
  계약: "계약",
  비교견적: "비교견적",
  사전승인: "사전승인",
  "사전 확인": "사전확인",
  집행: "기타",
  증빙: "증빙",
  기타: "기타",
};

/**
 * 화면의 일정 종류 → 서버 `구분`(결제전/결제후/집행).
 *
 * 주의: 「증빙」은 결제 '뒤' 에 하는 일입니다. 나머지는 결제 전 준비입니다.
 *    잘못 넣으면 상세의 「집행 준비 / 결제 후 진행」 두 묶음이 서로 바뀝니다.
 *
 * 주의: 「집행」만 세 번째 값으로 나갑니다. `adapt.ts` 가 `구분='집행'` 을 보고 캘린더
 *    배지를 구분해 표시합니다(`t.구분 === "집행" ? "집행" : …`).
 */
function 구분추정(type: ScheduleItem["type"]): "결제전" | "결제후" | "집행" {
  if (type === "집행") return "집행";
  return type === "증빙" ? "결제후" : "결제전";
}

export type 일정생성결과 =
  | { 결과: "저장됨"; taskId: string }
  | { 결과: "건너뜀" | "없음" | "실패" | "로그인필요" };

/**
 * 사용자가 직접 만든 일정을 서버에 만듭니다.
 *
 * 주의: 성공하면 '서버가 준 task_id' 를 돌려줍니다. 호출부는 화면의 임시 id를 그 값으로
 *    교체해야 합니다 — 안 그러면 그 일정의 '상태 변경'이 계속 저장되지 않습니다
 *    (`일정상태저장()` 이 숫자 id 만 서버 항목으로 봅니다).
 *
 * 「기타」(planId "other")는 계획 없는 일정으로 `POST /api/tasks` 에 만듭니다.
 */
export async function 일정생성(item: ScheduleItem): Promise<일정생성결과> {
  if (!API켜짐()) return { 결과: "건너뜀" };
  const 계획없음 = item.planId === "other";
  // 계획이 서버 것이 아니면 만들 수 없습니다 (mock 데이터 계획 등)
  if (!계획없음 && !서버항목인가(item.planId)) return { 결과: "건너뜀" };

  const 입력 = {
    항목: item.title.trim(),
    설명: item.memo?.trim() || undefined,
    구분: 구분추정(item.type),
    due_date: item.date || undefined,
    유형: 유형사전[item.type],
    상태: 상태사전[item.state],
  };
  try {
    const r = (await (계획없음 ? 일정추가(입력) : 할일추가(item.planId, 입력))) as {
      task_id?: number | string;
    };

    const id = r?.task_id;
    return id == null
      ? { 결과: "실패" }
      : { 결과: "저장됨", taskId: String(id) };
  } catch (e) {
    if (없음인가(e)) return { 결과: "없음" };
    if (e instanceof 인증오류 && e.상태 === 401) return { 결과: "로그인필요" };
    return { 결과: "실패" };
  }
}

/**
 * 화면이 만든 일정 목록을 서버에 반영합니다. **두 경우로 나뉩니다.**
 *
 * 주의: 이 구분이 이 함수의 존재 이유입니다. 섞으면 할일이 '중복' 됩니다.
 *
 *   taskId 있음  → 이미 서버에 있는 할일이다. 날짜만 PATCH 한다.
 *                 (상세의 「확인사항·증빙을 일정에 등록」이 이 경우 — 판정이 만든
 *                  plan_tasks 행에 due_date 를 추가하는 것이지 새 행이 아니다)
 *   taskId 없음  → 사용자가 직접 만든 일정이다. POST 로 '생성' 한다.
 *
 * 돌려주는 `바뀐id` 로 호출부가 화면의 임시 id 를 서버 task_id 로 교체합니다 —
 * 안 하면 그 일정의 상태 변경이 계속 저장되지 않습니다. `저장못함` 은 서버에 남지 않은
 * 일정의 화면 id 입니다(호출부가 화면에서 뺍니다).
 */
export async function 일정등록(
  새항목: ScheduleItem[],
): Promise<{ 바뀐id: Map<string, string>; 실패: number; 건너뜀: number; 로그인필요: boolean; 저장못함: Set<string> }> {
  const 바뀐id = new Map<string, string>();
  const 저장못함 = new Set<string>();
  let 실패 = 0;
  let 건너뜀 = 0;
  let 로그인필요 = false;
  if (!API켜짐()) return { 바뀐id, 실패, 건너뜀: 새항목.length, 로그인필요, 저장못함 };

  await Promise.all(
    새항목.map(async (item) => {
      // ── 경우 ① 이미 있는 할일 — 날짜만 추가한다
      if (item.taskId && 서버항목인가(item.taskId)) {
        try {
          await 일정수정(item.taskId, { due_date: item.date || null });
          바뀐id.set(item.id, item.taskId);
        } catch (e) {
          if (없음인가(e)) {
            건너뜀 += 1;
          } else {
            실패 += 1;
            저장못함.add(item.id);
          }
        }
        return;
      }
      // ── 경우 ② 사용자가 만든 일정 — 새로 만든다
      const r = await 일정생성(item);
      if (r.결과 === "저장됨") {
        바뀐id.set(item.id, r.taskId);
      } else if (r.결과 === "건너뜀") {
        건너뜀 += 1;
      } else {
        저장못함.add(item.id);
        if (r.결과 === "로그인필요") 로그인필요 = true;
        else 실패 += 1;
      }
    }),
  );
  return { 바뀐id, 실패, 건너뜀, 로그인필요, 저장못함 };
}
