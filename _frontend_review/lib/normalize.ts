"use client";

// 화면 8 「새 지출 계획 ① 기본 정보」 → 서버 정규화
//
// 주의: 서버에는 경로가 둘이고 '폼 경로' 가 프론트 정식 경로입니다.
//    폼 경로에서는 서버가 비목을 추측하지 '않습니다' — `비목후보: []` 를 줍니다.
//    (server/routes_judge.py `_live_정규화` 주석: "비목후보는 화면 9 에서 사용자가 직접 확정한다")
//    mock 서버는 후보를 채워 주기 때문에 개발 중에는 후보가 보입니다.
//    → 그래서 '후보가 비어도 정상' 으로 다뤄야 합니다.

import { 정규화SSE } from "./sse";
import { 분류정리 } from "./adapt";
import type { 비목후보, 심층질문항목, 부가세구분, 분류값 } from "./server-types";

export type 정규화입력값 = {
  품목: string;
  금액: number;
  용도: string;
  집행예정일?: string;
  거래처?: string;
  추가설명?: string;
  사업명?: string;
  부가세구분?: 부가세구분;
};

export type 정규화출력 = {
  비목후보: 비목후보[];
  /** 온톨로지 경로 서버만 줍니다. 비어 있으면 화면은 비목만 고르는 예전 모양입니다. */
  분류후보: 분류값[];
  정규화: Record<string, unknown>;
  질문원문: string;
  심층질문: 심층질문항목[];
};

/** 서버 계약은 최대 3개이고 맨 앞이 가장 가능성 높은 후보입니다. 순서를 바꾸지 않습니다. */
function 분류후보정리(값: unknown): 분류값[] {
  if (!Array.isArray(값)) return [];
  return 값.flatMap((항목) => {
    const 분류 = 분류정리(항목);
    return 분류 ? [분류] : [];
  }).slice(0, 3);
}

/**
 * 서버가 준 심층질문 배열을 화면이 믿고 쓸 수 있는 모양으로 다듬습니다.
 *
 * 주의: 「있으면 쓰고 없으면 안 그린다」— 여기서도 같은 원칙입니다. 모양이 안 맞는
 *    항목(code·질문문 없음)은 조용히 버립니다. 지어낸 질문을 화면에 올리지 않습니다.
 */
function 심층질문정리(값: unknown): 심층질문항목[] {
  if (!Array.isArray(값)) return [];
  return 값.flatMap((항목): 심층질문항목[] => {
    if (typeof 항목 !== "object" || 항목 === null) return [];
    const o = 항목 as Record<string, unknown>;
    const code = typeof o["code"] === "string" ? o["code"] : "";
    const 질문문 = typeof o["질문문"] === "string" ? o["질문문"] : "";
    if (!code || !질문문) return [];
    const 유형목록 = ["예아니오", "선택", "숫자", "텍스트"] as const;
    const 유형 = 유형목록.includes(o["유형"] as (typeof 유형목록)[number])
      ? (o["유형"] as (typeof 유형목록)[number])
      : "예아니오";
    const 선택지 = Array.isArray(o["선택지"])
      ? (o["선택지"] as unknown[]).flatMap((s) => {
          if (typeof s !== "object" || s === null) return [];
          const so = s as Record<string, unknown>;
          const sv = typeof so["값"] === "string" ? so["값"] : "";
          const sl = typeof so["라벨"] === "string" ? so["라벨"] : "";
          return sv && sl ? [{ 값: sv, 라벨: sl }] : [];
        })
      : [];
    const 근거원본 = (o["근거"] ?? {}) as Record<string, unknown>;
    const 근거 = {
      doc_id: typeof 근거원본["doc_id"] === "string" ? 근거원본["doc_id"] : "",
      조번호: typeof 근거원본["조번호"] === "string" ? 근거원본["조번호"] : "",
    };
    const 필요프로필필드 = Array.isArray(o["필요프로필필드"])
      ? (o["필요프로필필드"] as unknown[]).filter((x): x is string => typeof x === "string")
      : [];
    const 비목 = typeof o["비목"] === "string" ? o["비목"] : null;
    return [{ code, 비목, 질문문, 유형, 선택지, 근거, 필요프로필필드 }];
  });
}

/**
 * 서버가 준 비목 후보 배열을 화면이 믿고 쓸 수 있는 모양으로 다듬습니다.
 *
 * 주의: '있으면 쓰고 없으면 안 그린다' 가 원칙입니다. 서버가 `설명` 을 붙이기 시작하면
 *    코드를 한 줄도 안 고치고 화면에 뜨고, 그 전까지는 조용히 비어 있습니다.
 *
 * 주의: 신뢰도 내림차순으로 세웁니다. 서버가 이미 정렬해 보내면 결과가 같고, 안 보내도
 *    「가장 확신하는 후보」가 맨 앞에 옵니다 — 그 자리를 자동 선택에 쓰기 때문입니다.
 *    자바스크립트 정렬은 안정 정렬이라 신뢰도가 같으면 서버가 준 순서가 유지됩니다.
 */
function 후보정리(값: unknown): 비목후보[] {
  if (!Array.isArray(값)) return [];
  const 정리 = 값.flatMap((항목): 비목후보[] => {
    if (typeof 항목 !== "object" || 항목 === null) return [];
    const o = 항목 as Record<string, unknown>;
    const 비목 = typeof o["비목"] === "string" ? o["비목"].trim() : "";
    if (!비목) return [];
    const 설명 = typeof o["설명"] === "string" ? o["설명"].trim() : "";
    return [{
      비목,
      신뢰도: typeof o["신뢰도"] === "number" ? o["신뢰도"] : 0,
      설명: 설명 || undefined,
    }];
  });
  return 정리.sort((a, b) => b.신뢰도 - a.신뢰도);
}

export async function 정규화하기(
  입력: 정규화입력값,
  진행?: (설명: string) => void,
): Promise<정규화출력> {
  let 결과: Record<string, unknown> = {};
  let 오류: string | null = null;

  await 정규화SSE(입력, (이름, 값) => {
    const v = (값 ?? {}) as Record<string, unknown>;
    if (이름 === "진행") 진행?.(typeof v["설명"] === "string" ? v["설명"] : "정리하는 중");
    if (이름 === "결과") 결과 = v;
    // 주의: 실패가 HTTP 500 이 아니라 '오류 이벤트' 로 옵니다. try/catch 로는 안 잡힙니다.
    if (이름 === "오류") 오류 = typeof v["메시지"] === "string" ? v["메시지"] : "정리에 실패했습니다";
  });

  if (오류) throw new Error(오류);

  return {
    비목후보: 후보정리(결과["비목후보"]),
    분류후보: 분류후보정리(결과["분류후보"]),
    정규화: 결과,
    질문원문: typeof 결과["질문원문"] === "string" ? (결과["질문원문"] as string) : "",
    심층질문: 심층질문정리(결과["심층질문"]),
  };
}
