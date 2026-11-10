"use client";

// 화면 10 「추가 확인」 — 판정 전에 사용자에게 물을 확인 질문을 서버에서 받습니다.
//
// 확인 질문을 받지 못하면 실패를 알리고 다시 시도합니다.

import { SSE, 진행항목정리, type 진행항목 } from "./sse";
import { 용어풀이정리 } from "./adapt";
import type { 확인질문, 확인질문선택지, 확인질문근거, 추가입력칸, 용어풀이항목, 부가세구분 } from "./server-types";

export type 질문입력 = {
  사업명?: string | null;
  확정비목: string;
  확정세목?: string | null;
  부가세구분?: 부가세구분;
  세부유형?: string | null;
  품목: string;
  금액: number | null;
  용도: string;
  질문원문?: string;
  조사ID?: string;
  웹조사?: boolean;
  공개검색어?: string;
  판매URL?: string;
  조사답변?: 조사답변[];
};

/** `질문` 은 서버가 검사한 화면용 확인 문장(없으면 옛 서버), `질문종류`=기본 이면 유형 이름만 든 기본 문장입니다. 판정에는 `내용`(발췌)만 갑니다. */
export type 조사후보 = { id: string; 유형: string; 내용: string; 질문?: string; 질문종류?: string; 상태: string; 출처: { 제목: string; url: string; 발췌?: string }[] };
export type 조사답변 = { id: string; 답: "맞아요" | "아니에요" | "모르겠어요"; 정정?: string };
export type 조사결과 = { 상태?: string; 확인한정보?: (조사후보 & { 답?: string; 정정?: string })[]; 업체이용조건?: 조사후보[]; 미확인?: 조사후보[]; 부족정보?: string[]; 사용자확인?: { 항목: string; 질문: string }[] };

/** `질문` 과 `추가입력` 이 둘 다 비어 있으면 확인 화면 없이 바로 판정합니다. */
export type 질문결과 = {
  상태?: string;
  질문세트: string | null;
  질문: 확인질문[];
  추가입력: 추가입력칸[];
  용어풀이: 용어풀이항목[];
  조사ID?: string;
  조사후보?: 조사후보[];
  사용자확인?: { 항목: string; 질문: string }[];
  부족정보?: string[];
};

// 공개 검색을 포함한 요청 전체의 제한 시간입니다.
const 제한시간ms = 180000;
// 서버가 받는 질문원문 길이 상한입니다. 넘으면 422 가 나 질문 없이 넘어갑니다.
const 질문원문상한 = 2000;

const 기본선택지: 확인질문선택지[] = [
  { 라벨: "예", 값: "예" },
  { 라벨: "아니오", 값: "아니오" },
  { 라벨: "모르겠어요", 값: "모름" },
];

const 문자 = (o: Record<string, unknown>, 키: string): string =>
  typeof o[키] === "string" ? (o[키] as string).trim() : "";

/** 주의: 서버가 받는 답은 예·아니오·모름 세 값뿐입니다. 그 밖의 값은 버리고, 하나라도 빠지면 기본 세 버튼을 그립니다. */
function 선택지정리(값: unknown): 확인질문선택지[] {
  if (!Array.isArray(값)) return 기본선택지;
  const 정리 = 값.flatMap((s): 확인질문선택지[] => {
    if (typeof s !== "object" || s === null) return [];
    const o = s as Record<string, unknown>;
    const 라벨 = 문자(o, "라벨");
    const 값칸 = 문자(o, "값");
    return 라벨 && 기본선택지.some((b) => b.값 === 값칸) ? [{ 라벨, 값: 값칸 }] : [];
  });
  return 기본선택지.every((b) => 정리.some((s) => s.값 === b.값)) ? 정리 : 기본선택지;
}

function 근거정리(값: unknown): 확인질문근거 | null {
  if (typeof 값 !== "object" || 값 === null) return null;
  const o = 값 as Record<string, unknown>;
  const 원문 = 문자(o, "원문");
  if (!원문) return null;
  return {
    doc_id: 문자(o, "doc_id"),
    조번호: 문자(o, "조번호"),
    조제목: 문자(o, "조제목"),
    항호: 문자(o, "항호"),
    원문,
  };
}

function 질문정리(값: unknown): 확인질문[] {
  if (!Array.isArray(값)) return [];
  return 값.flatMap((항목): 확인질문[] => {
    if (typeof 항목 !== "object" || 항목 === null) return [];
    const o = 항목 as Record<string, unknown>;
    const id = 문자(o, "id");
    const 질문문 = 문자(o, "질문문");
    if (!id || !질문문) return [];
    return [{ id, 질문문, 이유: 문자(o, "이유"), 선택지: 선택지정리(o["선택지"]), 근거: 근거정리(o["근거"]) }];
  });
}

function 추가입력정리(값: unknown): 추가입력칸[] {
  if (!Array.isArray(값)) return [];
  return 값.flatMap((항목): 추가입력칸[] => {
    if (typeof 항목 !== "object" || 항목 === null) return [];
    const o = 항목 as Record<string, unknown>;
    const 키 = 문자(o, "키");
    const 라벨 = 문자(o, "라벨");
    if (!키 || !라벨) return [];
    const 단위 = 문자(o, "단위") || null;
    if (o["형식"] === "숫자") return [{ 키, 라벨, 단위, 형식: "숫자" }];
    if (o["형식"] !== "선택" || !Array.isArray(o["선택지"])) return [];
    const 선택지 = (o["선택지"] as unknown[]).filter((v): v is string => typeof v === "string" && !!v);
    return 선택지.length ? [{ 키, 라벨, 단위, 형식: "선택", 선택지 }] : [];
  });
}

/** 칸마다 미리 채울 글자. `수치` 에 키가 있을 때만 채우고, 사용자가 지우거나 고칠 수 있습니다. */
export function 추가입력초기값(칸들: 추가입력칸[], 수치: Record<string, unknown> | undefined): Record<string, string> {
  const 값들: Record<string, string> = {};
  for (const 칸 of 칸들) {
    const v = 수치?.[칸.키];
    if (typeof v !== "string" && typeof v !== "number") continue;
    const 글 = String(v).trim();
    if (글 && (칸.형식 === "숫자" ? Number.isFinite(Number(글)) : 칸.선택지?.includes(글))) 값들[칸.키] = 글;
  }
  return 값들;
}

/** 판정으로 보낼 `추가입력값`. 비운 칸은 보내지 않습니다. 서버가 그 한도를 비교하지 않고 결과의 조건으로 안내합니다. */
export function 추가입력값만들기(칸들: 추가입력칸[], 값들: Record<string, string>): Record<string, string | number> {
  const 보낼값: Record<string, string | number> = {};
  for (const 칸 of 칸들) {
    const 글 = (값들[칸.키] ?? "").trim();
    if (!글) continue;
    if (칸.형식 === "선택") {
      if (칸.선택지?.includes(글)) 보낼값[칸.키] = 글;
    } else if (Number.isFinite(Number(글))) {
      보낼값[칸.키] = Number(글);
    }
  }
  return 보낼값;
}

async function 질문요청(입력: 질문입력, on진행?: (항목: 진행항목) => void, signal?: AbortSignal): Promise<질문결과> {
  try {
    let 받은응답: Record<string, unknown> | null = null;
    await SSE("/api/questions", {
      ...입력,
      질문원문: 입력.질문원문?.slice(0, 질문원문상한),
    }, (이름, 값) => {
      if (이름 === "진행") { const 항목 = 진행항목정리(값); if (항목) on진행?.(항목); }
      if (이름 === "오류") throw new Error("질문을 받지 못했습니다.");
      if (이름 === "완료" && 값 && typeof 값 === "object") 받은응답 = 값 as Record<string, unknown>;
    }, { signal });
    const 응답 = 받은응답 as Record<string, unknown> | null;
    if (!응답 || !["있음", "없음", "바로판정", "조사확인"].includes(문자(응답, "상태"))) {
      throw new Error("확인 질문을 받지 못했습니다.");
    }
    const 조사상태 = 응답["조사"] as Record<string, unknown> | undefined;
    if (입력.웹조사 && (응답["상태"] === "실패" || 조사상태?.상태 === "실패")) {
      throw new Error("공개 정보 검색을 완료하지 못했습니다.");
    }
    if (입력.웹조사 && 입력.조사답변 === undefined && (응답["상태"] !== "조사확인" || !문자(응답, "조사ID"))) {
      throw new Error("공개 정보 확인을 시작하지 못했습니다.");
    }
    const 질문세트 = 문자(응답, "질문세트");
    // 주의: 질문세트가 없으면 서버가 답을 어느 질문에 맞출지 몰라 답이 판정에 들어가지 못합니다.
    //    답해도 쓰이지 않을 질문은 묻지 않습니다. 추가 입력 칸은 질문세트 없이도 판정에 들어갑니다.
    const 질문 = 질문세트 ? 질문정리(응답["질문"]) : [];
    const 추가입력 = 추가입력정리(응답["추가입력"]);
    // 주의: 온톨로지 경로는 질문이 비고 칸만 있을 수 있습니다. 질문만 보고 버리면 칸이 화면에 나오지 않습니다.
    const 조사 = 조사상태;
    const 후보 = Array.isArray(조사?.후보) ? 조사.후보 as 조사후보[] : [];
    const 사용자확인 = (Array.isArray(조사?.사용자확인) ? 조사.사용자확인 : []).flatMap((v): { 항목: string; 질문: string }[] => {
      if (!v || typeof v !== "object") return [];
      const o = v as Record<string, unknown>;
      const 항목 = 문자(o, "항목"), 질문 = 문자(o, "질문");
      return 항목 && 질문 ? [{ 항목, 질문 }] : [];
    });
    const 부족정보 = (Array.isArray(조사?.부족정보) ? 조사.부족정보 : []).filter((v): v is string => typeof v === "string" && !!v.trim());
    return { 상태: 문자(응답, "상태"), 질문세트: 질문세트 || null, 질문, 추가입력, 용어풀이: 용어풀이정리(응답["용어풀이"]), 조사ID: 문자(응답, "조사ID") || undefined, 조사후보: 후보, 사용자확인, 부족정보 };
  } catch (e) {
    throw e;
  }
}

/**
 * 제한 시간이 지나면 요청을 중단합니다. 공개 검색 실패는 호출자에게 전달합니다.
 */
export async function 질문가져오기(입력: 질문입력, on진행?: (항목: 진행항목) => void): Promise<질문결과> {
  const controller = new AbortController();
  const 타이머 = window.setTimeout(() => controller.abort(), 제한시간ms);
  try {
    return await 질문요청(입력, on진행, controller.signal);
  } finally {
    window.clearTimeout(타이머);
  }
}
