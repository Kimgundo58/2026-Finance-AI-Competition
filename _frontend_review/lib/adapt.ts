"use client";

// 서버 응답 → 화면이 쓰는 타입으로 번역합니다.
// 주의: 화면 코드(checkumait-app.tsx)를 안 고치려고 여기서 흡수합니다.

import type { 기준문서정보, ExpensePlan, PlanStatus, ScheduleItem, ChecklistItem, RuleItem } from "./types";
import type { 계획요약, 계획상세, 할일, 판정값, L3현재문서, 사용자답변항목, 부가세구분, 분류값, 용어풀이항목, 코드판정값, 확인한조건항목 } from "./server-types";
import { 초안읽기 } from "./inquiry-store";

/**
 * 판정 4종 → 배지 3종.
 * 주의: 배지는 3개지만 '설명 문구는 4개' 입니다 — 조건부와 판단불가가 같은 노랑인데
 *    사용자가 할 일이 다릅니다 (조건부=조건 채우기 / 판단불가=기관 문의).
 */
export function 판정을상태로(판정: 판정값): PlanStatus {
  switch (판정) {
    case "가능":
      return "특이사항 없음";
    case "조건부":
      return "확인 필요";
    case "판단불가":
      return "확인 필요";
    case "불가":
      return "위험";
    default:
      return "점검 전";
  }
}

/** 같은 노랑이라도 문구는 갈라야 합니다. 화면 11 에서 씁니다. */
export function 판정설명(판정: 판정값): string {
  switch (판정) {
    case "가능":
      return "규정상 문제되는 부분이 확인되지 않았습니다.";
    case "조건부":
      return "조건을 충족하면 집행할 수 있습니다. 아래 항목을 먼저 확인하세요.";
    case "판단불가":
      return "규정만으로는 결론을 낼 수 없습니다. 주관기관 문의가 필요합니다.";
    case "불가":
      return "규정에 어긋날 가능성이 높습니다. 집행 전에 반드시 확인하세요.";
    default:
      return "아직 점검하지 않았습니다.";
  }
}

/**
 * 배지 옆 '제목'. 같은 노란 배지(확인 필요)인 두 판정을 여기서 구분합니다.
 *
 * 주의: `status` 만 보면 「조건부」와 「판단불가」가 둘 다 “추가 확인이 필요합니다”가 됩니다.
 *    사용자는 그 문장을 읽고 무엇을 해야 하는지 알 수 없습니다.
 *    판정을 못 받은 계획(예시 데이터 등)은 `null` → 예전 문구로 돌아갑니다.
 */
export function 판정제목(판정: 판정값 | undefined, 비목: string): string | null {
  const 앞 = 비목 ? `${비목} ` : "";
  switch (판정) {
    case "가능":
      return `${앞}기준으로 특이사항이 없습니다.`;
    case "조건부":
      return `${앞}기준에서 확인할 조건이 있습니다.`;
    case "판단불가":
      return `${앞}기준만으로는 결론을 낼 수 없습니다.`;
    case "불가":
      return `${앞}기준에 어긋날 가능성이 높습니다.`;
    default:
      return null; // 화면이 기존 문구를 씁니다
  }
}

/**
 * '그래서 무엇을 해야 하는가'. 배지에도 요약에도 안 들어 있는 정보입니다.
 * 조건부는 사용자가 처리하고, 판단불가는 기관에 물어야 합니다 — 정반대입니다.
 * 주의: 온톨로지 경로의 조건부에는 채울 조건만 있는 것이 아닙니다(해당하면 안 되는 경우, 해당할 때만 필요한 것).
 *   그래서 「채우면 집행할 수 있다」고 쓰지 않습니다. 옛 경로 문구는 그대로입니다.
 * 주의: 물어볼 곳은 자금마다 다릅니다(창업활동자금은 멘토기관 담당자). 서버가 준 `문의처` 를 그대로 씁니다.
 * 주의: 금지 칸이 남아 있으면 채워서 되는 일이 아닙니다. 「채우면 집행할 수 있다」를 쓰지 않습니다.
 */
export type 조건칸상태값 = "해당" | "금지" | "보임" | "답변만" | "공통만" | "없음";

/** 판정 화면에 바로 보이는 조건 칸이 무엇인지. 공통 사항은 접혀 있어 「아래 항목」이라고 말할 수 없습니다.
 *  「답변만」= 남은 것이 선택 칸에 「해당 없음」이라고 답한 금지뿐입니다. 답은 진술이라 조건부지만, 채울 조건은 없습니다.
 *  「해당」= 선택 칸 답대로면 예외 없는 금지에 해당합니다. 「해당하지 않는지 확인」이라고 쓰면 사용자의 답과 어긋납니다. */
export function 조건칸상태(p: { 조건?: string[]; 기한조건?: string[]; 금지조건?: string[]; 해당시조건?: string[]; 공통사항?: string[]; 확인한조건?: 확인한조건항목[] }): 조건칸상태값 {
  if (p.금지조건?.length && p.확인한조건?.some((x) => x.해당문장.length)) return "해당";
  if (p.금지조건?.length) return "금지";
  if (p.조건?.length || p.기한조건?.length || p.해당시조건?.length) return "보임";
  if (p.확인한조건?.some((x) => x.문장.length)) return "답변만";
  return p.공통사항?.length ? "공통만" : "없음";
}

export function 행동문구(판정: 판정값 | undefined, 온톨로지경로 = false, 문의처: string | null | undefined = null, 칸상태: 조건칸상태값 = "보임"): string | null {
  switch (판정) {
    case "조건부":
      if (칸상태 === "해당") return `답하신 대로면 아래 안 되는 경우에 해당해요. 집행하기 전에 ${문의처 || "주관기관에"} 확인하세요.`;
      if (칸상태 === "금지") return "아래 안 되는 경우에 해당하지 않는지 먼저 확인한 뒤 집행하세요.";
      if (온톨로지경로 && 칸상태 === "답변만") return "아래 「확인한 내용」에 답하신 대로인지 확인한 뒤 집행하세요.";
      if (온톨로지경로 && 칸상태 === "공통만") return "아래 「사업 전체 공통 사항」을 펼쳐 확인한 뒤 집행하세요.";
      if (온톨로지경로 && 칸상태 === "없음") return `집행 전에 ${문의처 || "주관기관에"} 확인하세요.`;
      return 온톨로지경로 ? "아래 항목을 확인한 뒤 집행하세요." : "아래 조건을 채우면 집행할 수 있습니다.";
    case "판단불가":
      return `규정만으로는 결론이 나지 않습니다. 집행 전에 ${문의처 || "주관기관에"} 확인하세요.`;
    case "불가":
      return "이대로 집행하면 환수 대상이 될 수 있습니다. 반드시 먼저 확인하세요.";
    default:
      return null; // 「가능」과 미점검은 따로 시킬 일이 없습니다
  }
}

/** "2026-09-01T10:12:00+09:00" → "2026.09.01 10:12" */
export function 시각표기(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}.${p(d.getMonth() + 1)}.${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function 할일을체크로(t: 할일): ChecklistItem {
  return {
    id: String(t.task_id),
    label: t.항목,
    description: t.설명 ?? "",
    done: t.상태 === "완료",
  };
}

/**
 * L3 업로드 결과 문구의 '사실 부분'. 호출부가 자기 접두사("등록했습니다 — " 등)를 붙입니다.
 *
 * 주의: 2026-09-07 — 서버가 `파싱품질`(pending/pass/warn/fail)을 이미 채워 보내는데
 *    (`server/routes_l3.py:367`) 호출부 두 곳이 `조_건수` 만 보고 성공 톤을 고정해,
 *    데모 org 문서 4개(전부 `warn` — 안내문·양식이라 조·항 구조가 없어 단락으로
 *    떨어짐)를 올려도 "조 N건을 읽었습니다"만 보였습니다. 겁주지 않되 사실대로 말합니다.
 * 주의: 2026-11-03 — 스캔본은 서버가 OCR 로 읽습니다(`추출방식="ocr"`, 파싱품질은 항상 warn).
 *    이때는 '조·항 구조가 없다' 가 아니라 '잘못 읽었을 수 있다' 가 사실이므로 따로 말합니다.
 */
export function L3등록사실(
  조_건수: number | null | undefined,
  파싱품질: "pending" | "pass" | "warn" | "fail" | "review" | null | undefined,
  추출방식?: string | null,
): string {
  if (파싱품질 === "fail") {
    return "문서를 읽지 못했습니다. 판정에는 반영되지 않습니다.";
  }
  if (!조_건수) return "문서를 읽는 중입니다. 끝나면 판정에 반영됩니다.";
  if (추출방식 === "ocr") {
    return `스캔본을 OCR 로 읽어 ${조_건수}개 항목을 등록했습니다. 금액·조번호를 잘못 읽었을 수 있어 이 문서를 근거로 한 '가능'은 '조건부'로 안내합니다.`;
  }
  if (파싱품질 === "warn") {
    return `${조_건수}개 항목을 읽었습니다. 이 문서는 조·항 구조가 없어 항목 단위로 나눴습니다.`;
  }
  return `조 ${조_건수}건을 읽었습니다.`;
}

/** 마이페이지 「주관기관 세부 안내」가 그릴 요약 — `L3현재문서요약짓기()` 결과. */
export type L3현재문서요약 = {
  /** 판정에 쓰는 파일명 — pass/warn 문서. 없으면 `null`(기관 세부기준 없이 판정). */
  파일명: string | null;
  /** 대표 파일이 warn 이면 그 사실 문구(`L3등록사실()` 과 같은 톤). 아니면 `null`. */
  안내: string | null;
  /** 대표 파일을 등록한 날(YYYY-MM-DD). 모르면 `null`. */
  등록일: string | null;
  /** 대표 파일이 스캔본을 OCR 로 읽은 문서인가(`추출방식="ocr"`). 금액·조번호를 잘못 읽었을 수 있습니다. */
  ocr: boolean;
  /** `파싱품질='fail'` 인 문서들 — 판정에 안 쓰이지만 '숨기지 않고' 따로 보여줄 목록. */
  실패목록: L3현재문서[];
  /** 서버가 아직 읽고 있는 문서들(`pending`). 다 읽어야 판정에 쓰이므로 「적용 중」에 두지 않습니다. */
  처리중목록: L3현재문서[];
  /** 다 읽었지만 세부기준이 맞는지 사용자가 골라야 하는 문서(`review`). 고르기 전에는 판정에 쓰지 않습니다. */
  확인대기목록: L3현재문서[];
  /** 서버에서 목록을 읽지 못했는가. 문서가 없는 것과 구분합니다. */
  조회실패: boolean;
  /** 대표 파일의 조문 연결(서버가 연결을 쓸 때만). `null` 이면 연결 화면을 보이지 않습니다. */
  연결문서?: L3현재문서 | null;
};

/** 마이페이지 「조문 연결」 줄에 쓰는 셈. 확인 전 조 수 · 옛 판 문구가 있는 조 수. */
export function 조문연결셈(문서: L3현재문서) {
  const 목록 = 문서.연결 ?? [];
  return {
    전체: 목록.length,
    확인전: 목록.filter((x) => x.상태 !== "확인").length,
    옛판: 목록.filter((x) => x.옛판문구.length > 0).length,
  };
}

/**
 * `GET /api/l3/current` 응답(여러 건 가능) → 화면 요약 한 개.
 *
 * 주의: 2026-09-07 — 데모 org 는 문서 3건 중 1건이 `파싱품질='fail'` 입니다(실측).
 *    「적용 중」자리에 fail 문서까지 나란히 보여주면 사용자가 "반영되고 있다"고
 *    오해합니다. 그렇다고 숨기면 왜 적용되지 않는지 알 수 없습니다 — 그래서 대표 하나만
 *    「적용 중」에 놓고, fail 은 `실패목록`으로 따로 분리해 둘 다 보여줍니다.
 * 주의: 2026-11-03 — 기관당 판정에 쓰는 문서는 하나이고 서버가 그 문서를 맨 앞에 줍니다. 대표는
 *    pass/warn 문서만입니다. 읽는 중(pending)인 문서는 아직 판정에 쓰이지 않으므로 「적용 중」에
 *    두지 않고 `처리중목록`으로 나눕니다(예전엔 적용 문서가 없으면 pending 이 「적용 중」으로 보였습니다).
 */
export function L3현재문서요약짓기(목록: L3현재문서[]): L3현재문서요약 {
  const 실패목록 = 목록.filter((d) => d.파싱품질 === "fail");
  const 처리중목록 = 목록.filter((d) => d.파싱품질 === "pending" || d.파싱품질 === null);
  const 대표 = 목록.find((d) => d.파싱품질 === "pass" || d.파싱품질 === "warn") ?? null;
  return {
    파일명: 대표?.원본파일명 ?? null,
    안내: 대표 && 대표.파싱품질 === "warn" ? L3등록사실(대표.조_건수, 대표.파싱품질, 대표.추출방식) : null,
    등록일: 대표?.등록일 ?? null,
    ocr: 대표?.추출방식 === "ocr",
    실패목록,
    처리중목록,
    확인대기목록: 목록.filter((d) => d.파싱품질 === "review"),
    조회실패: false,
    연결문서: 대표 && 대표.연결 ? 대표 : null,
  };
}

/** 내부 doc_id → 사람이 읽는 문서명.
 *
 * 주의: 화면에 `L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223` 이 그대로
 *    나가고 있었습니다. `L1_`(계층 접두)·밑줄·끝의 8자리 날짜는 '우리 내부 표기' 라
 *    사용자에게는 뜻이 없습니다. 근거를 보여주는 자리에서 제일 먼저 눈에 띄는 줄이라
 *    이것 하나로 화면이 '내부 도구' 처럼 보입니다.
 * 주의: 문서를 '바꾸는' 게 아니라 '읽는 이름' 만 만듭니다 — doc_id 자체는 인용 대조에
 *    그대로 쓰입니다. 규칙에 안 맞는 id 는 건드리지 않고 그대로 돌려줍니다.
 */
export function 문서명보기(docId: string): string {
  if (!docId) return "";
  let s = docId.replace(/^L[123]_/, "");
  s = s.replace(/_(\d{4})(\d{2})(\d{2})$/, " ($1.$2.$3)");
  s = s.replace(/제(\d+)차개정/, "제$1차 개정");
  s = s.replace(/_/g, " ");
  return s.replace(/\s+/g, " ").trim();
}

// 조문 항 마커. `scripts/llm_validate.py:_항마커` 와 같은 목록 — 서버가 '조 전체' 를
// 잘라주지 않은 경우(아래 항추출) 프론트가 같은 규칙으로 다시 잘라야 짝이 맞는다.
const 항마커 = [
  "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
  "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳",
];

/** 조 본문에서 `항호`(예: "③" · "③④")가 가리키는 항만 잘라낸다.
 *
 * 주의: 2026-09-07 — `scripts/llm_validate.py:_항_추출()` 과 같은 규칙을 프론트에
 *    옮긴 것. 서버는 `종류="article"`(조문 전체 소스)일 때만 이 자르기를 하고,
 *    `종류="chunk"`(청크가 이미 항 단위라고 가정)는 자르지 않는다(`llm_schema.py:144`
 *    주석). 그런데 운영 화면(제38조 외주용역비)에서는 그 가정이 깨져 청크가 조 전체
 *    (10개 항)를 그대로 담고 있었다 — `항호`(③④)는 맞게 왔는데 `원문`은 안 잘렸다.
 *    백엔드를 고치는 대신(이 작업 범위 밖) 프론트가 같은 규칙으로 한 번 더 잘라 방어한다.
 * 주의: **자르기지 요약이 아니다** — 마커를 못 찾으면 원문을 그대로 돌려준다(「인용은
 *    생성이 아니라 추출」, `scripts/llm_validate.py:93`와 같은 원칙). 지어내지 않는다.
 */
export function 항추출(본문: string, 항호: string): string {
  if (!본문) return "";
  const 마커목록 = Array.from(항호 || "").filter((ch) => 항마커.includes(ch));
  if (!마커목록.length) return 본문;
  const k = 본문.indexOf(마커목록[0]);
  if (k < 0) return 본문;
  const 끝마커위치 = 본문.indexOf(마커목록[마커목록.length - 1], k);
  let end = 본문.length;
  if (끝마커위치 >= 0) {
    for (const m of 항마커) {
      const i = 본문.indexOf(m, 끝마커위치 + 1);
      if (i > 0 && i < end) end = i;
    }
  }
  const 잘린 = 본문.slice(k, end).trim();
  return 잘린 || 본문;
}

/** 서버 `인용[]`(원시 딕셔너리 배열) → 화면에 낼 `RuleItem[]`.
 *
 * 주의: 2026-09-06 — 여기서 읽던 여섯 키(제목·조·문서·출처·본문·설명)가 **서버 스키마에
 *    하나도 없었습니다.** 기준 코드는 `scripts/llm_schema.py:208 class 인용` 이고
 *    s번호 · doc_id · 조번호 · 조제목 · 항호 · 원문 · 원문범위 · version · extraction 입니다.
 *    그래서 운영 서버 판정에서 「적용 근거 2건」인데 각 줄이 '근거 조항'(폴백) + 빈칸으로
 *    나갔습니다 — 조문 내용이 전혀 보이지 않았습니다 (2026-09-06 운영 화면 확인).
 *    주의: `lib/judge.ts` 는 '이 함수를 안 쓰고' 같은 매핑을 자기 안에 따로 두고 있었습니다
 *       — 같은 서버 응답을 두 파일이 서로 다른 키로 읽던 문제가 재발했습니다(2026-09-07
 *       운영 화면 확인). `judge.ts` 도 이 함수 하나로 모읍니다.
 * 주의: 2026-09-07 — **같은 (doc_id, 조번호) 는 한 건으로 합칩니다.** 운영 화면에서
 *    「제38조 외주용역비」가 항호만 다른 두 건으로 와서, 조 전체(10개 항) 원문이 그대로
 *    두 번 표시됐습니다 — 실제로 해당하는 건 ③·④항뿐이었습니다. 항호를 합쳐 제목에 붙이고,
 *    본문은 `항추출()`로 해당 항만 남깁니다(원문에 없는 문장은 만들지 않습니다).
 *    쪽으로 표시하는 문서는 같은 「14쪽」에 소제목이 둘일 수 있어 조제목까지 같아야 합칩니다.
 */
export function 인용정리(인용들: Record<string, unknown>[]): RuleItem[] {
  const 문자 = (c: Record<string, unknown>, k: string) =>
    typeof c[k] === "string" ? (c[k] as string) : "";

  const 그룹: Map<
    string,
    { 조번호: string; 조제목: string; doc_id: string; version: string; 항호들: string[]; 조각들: string[] }
  > = new Map();
  const 순서: string[] = [];

  for (const c of 인용들) {
    const doc_id = 문자(c, "doc_id");
    const 조번호 = 문자(c, "조번호");
    const 항호 = 문자(c, "항호");
    const key = `${doc_id}::${조번호}::${문자(c, "조제목")}`;
    if (!그룹.has(key)) {
      그룹.set(key, {
        조번호,
        조제목: 문자(c, "조제목"),
        doc_id,
        version: 문자(c, "version"),
        항호들: [],
        조각들: [],
      });
      순서.push(key);
    }
    const g = 그룹.get(key)!;
    if (항호 && !g.항호들.includes(항호)) g.항호들.push(항호);
    const 조각 = 항추출(문자(c, "원문"), 항호);
    if (조각 && !g.조각들.includes(조각)) g.조각들.push(조각);
  }

  return 순서.map((key) => {
    const g = 그룹.get(key)!;
    // 항 기호(①②)는 이어 쓰고, 별지 칸 이름(지급수수료 유의사항)은 사이를 띄웁니다
    const 항호결합 = g.항호들.join(g.항호들.every((h) => h.length <= 2) ? "" : " · ");
    return {
      title:
        [g.조번호, g.조제목].filter(Boolean).join(" ") +
          (항호결합 ? ` ${항호결합}` : "") || "근거 조항",
      source: [문서명보기(g.doc_id), g.version].filter(Boolean).join(" · "),
      description: g.조각들.join("\n"),
    };
  });
}

/** 서버 `기준문서` → 화면 값. 모양이 다르면 없는 것으로 보고 안내를 그리지 않습니다. */
export function 기준문서정리(값: unknown): 기준문서정보 | null {
  if (!값 || typeof 값 !== "object") return null;
  const v = 값 as Record<string, unknown>;
  if (v.상태 !== "없음" && v.상태 !== "적용" && v.상태 !== "읽기실패" && v.상태 !== "확인대기") return null;
  return {
    상태: v.상태,
    문서명: typeof v.문서명 === "string" ? v.문서명 : null,
    대기문서명: typeof v.대기문서명 === "string" ? v.대기문서명 : null,
  };
}

/** 서버 `사용자답변[]` → 화면에 낼 질문과 답. 모양이 맞지 않는 항목은 버립니다. */
export function 사용자답변정리(값: unknown): 사용자답변항목[] {
  if (!Array.isArray(값)) return [];
  return 값.flatMap((항목): 사용자답변항목[] => {
    if (typeof 항목 !== "object" || 항목 === null) return [];
    const o = 항목 as Record<string, unknown>;
    const 질문문 = typeof o["질문문"] === "string" ? o["질문문"] : "";
    const 답 = typeof o["답"] === "string" ? o["답"] : "";
    return 질문문 && 답 ? [{ 질문문, 답 }] : [];
  });
}

/** 서버 `확인한조건[]` → 「확인한 내용」에 낼 선택 칸 답. 항목 · 답이 없으면 버립니다. */
export function 확인한조건정리(값: unknown): 확인한조건항목[] {
  if (!Array.isArray(값)) return [];
  return 값.flatMap((항목): 확인한조건항목[] => {
    if (typeof 항목 !== "object" || 항목 === null) return [];
    const o = 항목 as Record<string, unknown>;
    const 이름 = typeof o["항목"] === "string" ? o["항목"].trim() : "";
    const 답 = typeof o["답"] === "string" ? o["답"].trim() : "";
    return 이름 && 답 ? [{ 항목: 이름, 답, 문장: 문장목록정리(o["문장"]), 해당문장: 문장목록정리(o["해당문장"]) }] : [];
  });
}

/** 서버 값 「모름」은 사용자가 누른 버튼 그대로 「모르겠어요」로 보여 줍니다. */
export function 답표기(답: string): string {
  return 답 === "모름" ? "모르겠어요" : 답;
}

/** 말 끝 받침에 맞는 조사 「로」·「으로」. ㄹ 받침과 받침 없는 말은 「로」입니다. 세목은 받침으로 끝나기도 합니다(시험·인증). */
export function 로조사(말: string): string {
  const 끝 = 말.trim().charCodeAt(말.trim().length - 1);
  if (!(끝 >= 0xac00 && 끝 <= 0xd7a3)) return "로";
  const 받침 = (끝 - 0xac00) % 28;
  return 받침 === 0 || 받침 === 8 ? "로" : "으로";
}

/** 서버 `분류` · `분류후보[]` 한 건. 비목·표시문이 없으면 버립니다. */
export function 분류정리(값: unknown): 분류값 | null {
  if (typeof 값 !== "object" || 값 === null) return null;
  const o = 값 as Record<string, unknown>;
  const 비목 = typeof o["비목"] === "string" ? o["비목"].trim() : "";
  const 표시문 = typeof o["표시문"] === "string" ? o["표시문"].trim() : "";
  if (!비목 || !표시문) return null;
  const 세목 = typeof o["세목"] === "string" && o["세목"].trim() ? o["세목"].trim() : null;
  return { 비목, 세목, 표시문 };
}

/** 서버 `조건` · `근거경로`. 글자가 있는 항목만 남깁니다. */
export function 문장목록정리(값: unknown): string[] {
  if (!Array.isArray(값)) return [];
  return 값.flatMap((v) => (typeof v === "string" && v.trim() ? [v.trim()] : []));
}

/** 서버 `용어풀이`. 용어와 풀이가 다 있는 것만 남깁니다. */
export function 용어풀이정리(값: unknown): 용어풀이항목[] {
  if (!Array.isArray(값)) return [];
  return 값.flatMap((항목): 용어풀이항목[] => {
    if (typeof 항목 !== "object" || 항목 === null) return [];
    const o = 항목 as Record<string, unknown>;
    const 용어 = typeof o["용어"] === "string" ? o["용어"].trim() : "";
    const 풀이 = typeof o["풀이"] === "string" ? o["풀이"].trim() : "";
    return 용어 && 풀이 ? [{ 용어, 풀이 }] : [];
  });
}

/** 서버 `코드판정`. 모르는 `종류` 는 그릴 문구가 없으므로 없는 것으로 봅니다. */
export function 코드판정정리(값: unknown): 코드판정값 | null {
  if (typeof 값 !== "object" || 값 === null) return null;
  const o = 값 as Record<string, unknown>;
  const 글 = (k: string) => (typeof o[k] === "string" && (o[k] as string).trim() ? (o[k] as string).trim() : null);
  const 종류 = o["종류"];
  if (종류 !== "금지" && 종류 !== "한도초과" && 종류 !== "부가세기준미정") return null;
  return { 종류, 비교값: 글("비교값"), 한도: 글("한도"), 발췌: 글("발췌") };
}

const 부가세값인가 = (v: unknown): v is 부가세구분 => v === "포함" || v === "공급가액" || v === "모름";

/** 코드가 만든 부가세 확인 질문의 id. `question_generate.py::_새경로_준비` 가 정합니다. */
export const 부가세질문id = "VAT";

/**
 * 입력의 부가세 구분에 부가세 확인 질문의 답을 더한 값.
 * 주의: 서버 `question_generate.py::_부가세_값` 과 같은 표입니다. 서버가 판정에 쓰는 값과 화면에 적는 값이 어긋나지 않게 합니다.
 */
export function 부가세확정(입력: 부가세구분 | undefined, 답: unknown): 부가세구분 | undefined {
  if (입력 === "포함" || 입력 === "공급가액") return 입력;
  return 답 === "예" ? "포함" : 답 === "아니오" ? "공급가액" : 입력;
}

/** 금액 옆에 붙이는 말. 모르는 계획에는 아무 말도 붙이지 않습니다. */
export function 부가세표기(값: 부가세구분 | undefined): string {
  return 값 === "포함"
    ? " (부가세 포함)"
    : 값 === "공급가액"
      ? " (공급가액, 부가세 별도)"
      : 값 === "모름"
        ? " (부가세 포함 여부 모름)"
        : "";
}

/**
 * 계획의 `정규화` 에 저장해 둔 온톨로지 경로 값. 계획 생성 API 에 따로 받는 칸이 없어 여기에 둡니다.
 * 주의: 옛 경로 계획에는 이 키들이 없어 전부 undefined 입니다.
 */
export function 정규화저장값(정규화: Record<string, unknown> | null | undefined): {
  부가세구분?: 부가세구분;
  확정세목?: string;
  수치?: Record<string, unknown>;
} {
  const 수치 = 정규화?.["수치"];
  const 수치표 = typeof 수치 === "object" && 수치 !== null ? (수치 as Record<string, unknown>) : undefined;
  const 부가세 = 수치표?.["부가세구분"];
  const 세목 = 정규화?.["확정세목"];
  return {
    부가세구분: 부가세값인가(부가세) ? 부가세 : undefined,
    확정세목: typeof 세목 === "string" && 세목 ? 세목 : undefined,
    수치: 수치표,
  };
}

/** 목록 한 줄 → ExpensePlan (상세 필드는 비어 있습니다) */
export function 요약을계획으로(s: 계획요약): ExpensePlan {
  return {
    id: String(s.plan_id),
    name: s.제목 ?? "(제목 없음)",
    purpose: "",
    amount: s.금액 ?? 0,
    plannedDate: s.집행예정일 ?? "",
    category: s.확정비목 ?? "",
    vendor: "",
    status: 판정을상태로(s.판정),
    판정: s.판정 ?? null,       // 주의: status 로 합쳐지기 '전' 의 원래 값 (조건부/판단불가 구분용)
    nextAction: "",
    updatedAt: 시각표기(s.updated_at),
    aiSummary: "",
    aiChecks: [],
    evidence: [],
    rules: [],
    단계요약: s.단계요약,
    현재단계: s.현재단계 ?? null,
    actualDate: s.실제지출일 ?? undefined,
    actualAmount: s.실제지출액 ?? undefined,
    executionStatus: s.실제지출일 ? "결제 완료" : undefined,
  };
}

/** 상세 → ExpensePlan (판정·할일·근거까지 채웁니다) */
export function 상세를계획으로(d: 계획상세): ExpensePlan {
  const 기본 = 요약을계획으로(d);
  const 할일들 = d.할일 ?? [];
  const 결제전 = 할일들.filter((t) => t.구분 === "결제전");
  const 결제후 = 할일들.filter((t) => t.구분 === "결제후");
  const 남은것 = 할일들.find((t) => t.상태 !== "완료");
  const 상세 = d.판정상세 ?? null;
  const 인용들 = Array.isArray(상세?.인용) ? (상세?.인용 as Record<string, unknown>[]) : [];

  return {
    ...기본,
    purpose: d.용도 ?? "",
    vendor: d.거래처 ?? "",
    nextAction: 남은것?.항목 ?? "",
    aiSummary:
      (typeof 상세?.요약 === "string" ? 상세.요약 : "") || 판정설명(d.판정),
    // 주의: 결제전이 없으면 「집행」까지 포함해 보여줍니다 — 빈 화면보다 낫습니다
    // 주의: 집행 줄은 일정 탭의 줄이고 확인 항목이 아닙니다.
    aiChecks: (결제전.length ? 결제전 : 할일들.filter((t) => t.구분 !== "집행")).map(할일을체크로),
    evidence: 결제후.map(할일을체크로),
    rules: 인용정리(인용들),
    /**
     * 주의: 서버는 '아직' 초안을 안 돌려줍니다 — `_live_상세` 가 읽는 decisions 컬럼에
     *    문의초안이 없습니다(판정 직후 SSE 로만 전달됩니다). 그래서 이 탭이 받아 둔
     *    값으로 채웁니다. 백엔드가 `판정상세.문의초안` 을 포함하기 시작하면 그쪽이 우선합니다.
     *
     * 주의: 「판단불가」일 때만 씁니다. 재판정으로 「가능」이 되었는데 옛 초안이 남아
     *    있으면, 지금 판정과 안 맞는 문의를 보내게 됩니다.
     */
    문의초안:
      (typeof 상세?.문의초안 === "string" ? 상세.문의초안 : null) ??
      (d.판정 === "판단불가" ? 초안읽기(d.plan_id) : null),
    사용자답변: 사용자답변정리(d.사용자답변),
    ...정규화저장값(d.정규화),
    분류: 분류정리(상세?.분류),
    근거경로: 문장목록정리(상세?.근거경로),
    코드판정: 코드판정정리(상세?.코드판정),
    조건: 문장목록정리(상세?.조건),
    기한조건: 문장목록정리(상세?.기한조건),
    금지조건: 문장목록정리(상세?.금지조건),
    해당시조건: 문장목록정리(상세?.해당시조건),
    공통사항: 문장목록정리(상세?.공통사항),
    확인한조건: 확인한조건정리(상세?.확인한조건),
    조사: 상세?.조사 ?? null,
    용어풀이: 용어풀이정리(상세?.용어풀이),
    문의처: typeof 상세?.문의처 === "string" ? 상세.문의처 : null,
    기준문서: 기준문서정리(상세?.기준문서),
    latestDecisionId: d.latest_decision_id ?? null,
    내피드백: d.내피드백 ?? null,
    팀평가: d.팀평가 ?? [],
  };
}

const 유형표: Record<string, ScheduleItem["type"]> = {
  계약: "계약",
  비교견적: "비교견적",
  사전승인: "사전승인",
  사전확인: "사전 확인",
  증빙: "증빙",
  기타: "기타",
};

const 상태표: Record<string, ScheduleItem["state"]> = {
  준비필요: "준비 필요",
  집행예정: "집행 예정",
  완료: "완료",
};

/**
 * 주의: 서버 할일의 `due_date` 는 '없을 수 있습니다' — 규정에 기한 근거가 없으면 서버가
 *    날짜를 만들지 않습니다(체크리스트에만 있고 캘린더에는 안 올린다는 뜻, 서버 models.py 주석).
 *    예전에는 여기서 10/01~10/07 을 지어 넣었는데, 규정에 없는 날짜이고 지나면 모두 기한이
 *    지난 일로 보였습니다. 이제 빈 문자열로 두고, 화면은 「날짜 미정」으로 표시합니다
 *    (`월일표기("")` → "—", 캘린더에는 안 올라감).
 */
export function 할일을일정으로(t: 할일): ScheduleItem {
  return {
    id: String(t.task_id),
    taskId: String(t.task_id),      // 주의: 상세 체크박스와 이어지는 키
    // 주의: 계획 없는 일정은 일정 입력 창의 「기타」 값("other")과 같게 맞춥니다.
    planId: t.plan_id === null ? "other" : String(t.plan_id),
    출처: t.출처,
    지출일연동: !!t.집행예정일_연동,
    작성자: t.작성자 ?? null,
    title: t.항목,
    date: t.due_date || "",
    type: t.구분 === "집행" ? "집행" : (유형표[t.유형] ?? "기타"),
    state: 상태표[t.상태] ?? "준비 필요",
    memo: t.설명 ?? undefined,
  };
}
